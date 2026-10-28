"""Architecture guard: EVERY CI pytest command carries the unraisable-warning gate.

A leaked never-awaited coroutine or unclosed resource surfaces at garbage
collection as ``PytestUnraisableExceptionWarning``. At the ``default`` action a
large enough flood of them wedges pytest's terminal writer
(``BlockingIOError: [Errno 11]``) and takes a CI job down with no test named -
the 2026-10-21 merge-queue outage. ``tests/conftest.py::pytest_runtest_setup``
escalates the class to an error from inside each gated item, and every CI pytest
command carries the matching ``-W`` flag plus ``--disable-warnings`` as defence
in depth.

The gate only works if the warnings plugin is loaded. Three suites (BDD, the
architecture job, the docker harness job) were structurally blind because they
ran ``-p no:warnings``, which disables that plugin outright. The in-process hook
covers the *suite*; this guard covers the *command*, so a new or edited command
cannot silently return to blind.

The rule enforced here is deliberately broader than "commands that name a gated
tree": every pytest invocation on a CI surface must carry the gate. A command
that passes a computed file list, a shell variable, or a script's own argv
cannot be classified statically, so classification is not attempted - the
invocation itself is the trigger.

Surfaces scanned:

- every ``run:`` block in ``.github/workflows/*.yml`` and composite actions
  under ``.github/actions/**`` (YAML block scalars, backslash continuations and
  YAML quoting are all handled);
- every ``*.sh`` under ``.github/scripts/`` and ``scripts/``;
- every ``*.py`` under ``.github/scripts/`` and ``scripts/`` (pytest argv lists
  are located with ``ast``, and a starred shared-constant tuple is resolved);
- every ``entry:`` in ``.pre-commit-config.yaml`` that invokes pytest.

Prose is not a command: an ``echo``/``printf`` line that mentions pytest and a
``#`` comment (full-line or trailing) are ignored, and a flag that appears only
in a comment does not count.

``EXEMPTIONS`` is EMPTY by design. Every current invocation can carry the gate;
if one genuinely cannot, add it with a written reason and expect review to
challenge it - an exemption is a hole in the gate.

Prove-it-fails: deleting ``-W "error::pytest.PytestUnraisableExceptionWarning"``
from any real command fails the matching test naming that command; adding
``-p no:warnings`` to one fails it too.
"""

from __future__ import annotations

import ast
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

import tests.conftest as _root_conftest

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
WORKFLOWS_DIR = REPO / ".github" / "workflows"
ACTIONS_DIR = REPO / ".github" / "actions"
CI_SCRIPTS_DIR = REPO / ".github" / "scripts"
SCRIPTS_DIR = REPO / "scripts"
PRE_COMMIT_CONFIG = REPO / ".pre-commit-config.yaml"

#: The byte-identical escalation every CI command and the in-process hook share.
ESCALATION = _root_conftest.UNRAISABLE_ESCALATION
DISABLE_WARNINGS = "--disable-warnings"

#: pytest commands exempt from the rule, keyed by location prefix. EMPTY BY
#: DESIGN - no current invocation needs one. An entry here must carry a reason
#: and is a deliberate weakening of the gate, never a convenience.
EXEMPTIONS: dict[str, str] = {}

_PYTEST = "pytest"
_QUOTES = "\"'"
#: A segment whose first word is one of these only PRINTS text mentioning
#: pytest; it does not invoke it.
_SHELL_NON_EXECUTORS = frozenset({"echo", "printf", ":", "true", "false"})
#: Shell command separators that start a new simple command. ``||``/``&&`` are
#: matched before the single-character forms.
_SHELL_OPERATORS = ("&&", "||", ";", "|")


@dataclass(frozen=True)
class PytestCommand:
    """One discovered pytest invocation and whether it carries the gate."""

    location: str
    text: str
    has_escalation: bool
    has_disable_warnings: bool
    disables_warnings_plugin: bool

    @property
    def ok(self) -> bool:
        """True when the invocation can surface a leaked-resource warning."""
        return self.has_escalation and self.has_disable_warnings and not self.disables_warnings_plugin

    def problem(self) -> str:
        """A reviewer-facing description of every gate element the command lost."""
        missing = []
        if self.disables_warnings_plugin:
            missing.append("passes `-p no:warnings`, disabling the warnings plugin the gate rides on")
        if not self.has_escalation:
            missing.append(f'lacks `-W "{ESCALATION}"`')
        if not self.has_disable_warnings:
            missing.append(f"lacks `{DISABLE_WARNINGS}`")
        return f"{self.location} invokes pytest but " + "; ".join(missing) + f"\n  command: {self.text}"


def _strip_shell_comment(line: str) -> str:
    """Remove a trailing ``#`` comment without touching ``#`` inside quotes."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in _QUOTES:
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _logical_commands(block: str) -> list[str]:
    """Split a shell block into logical commands, joining ``\\`` continuations.

    Blank lines and comment-only lines are dropped. A continuation is joined
    BEFORE tokenising, because several CI commands put the gate on the second
    or third physical line.
    """
    commands: list[str] = []
    pending = ""
    for raw in block.splitlines():
        line = _strip_shell_comment(raw).rstrip()
        if not line.strip():
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        pending += line
        commands.append(pending.strip())
        pending = ""
    if pending.strip():
        commands.append(pending.strip())
    return commands


def _split_shell_segments(command: str) -> list[str]:
    """Split on unquoted ``&&``/``||``/``;``/``|`` so an echo cannot hide a run."""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in _QUOTES:
            quote = char
            current.append(char)
            index += 1
            continue
        pair = command[index : index + 2]
        if pair in _SHELL_OPERATORS:
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if char in (";", "|"):
            segments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def _tokens(segment: str) -> list[str]:
    """Tokenise a shell segment, tolerating an unbalanced quote in a fragment.

    A multi-line quoted body elsewhere in the same step can leave a logical
    fragment unparseable by ``shlex``; the gate tokens are themselves unquoted,
    so a whitespace split is an adequate conservative fallback.
    """
    try:
        raw = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        raw = segment.split()
    return [token.strip(_QUOTES) for token in raw]


def _has_escalation(tokens: list[str]) -> bool:
    """True when the ``-W`` escalation is present, separate or attached."""
    for index, token in enumerate(tokens):
        if token == "-W":
            if index + 1 < len(tokens) and tokens[index + 1] == ESCALATION:
                return True
        elif token.startswith("-W") and ESCALATION in token[2:]:
            return True
    return False


def _disables_warnings_plugin(tokens: list[str]) -> bool:
    """True for ``-p no:warnings`` (both spellings), which blinds the gate."""
    if "-pno:warnings" in tokens:
        return True
    return any(tokens[index] == "-p" and tokens[index + 1] == "no:warnings" for index in range(len(tokens) - 1))


def _shell_pytest_commands(location: str, block: str) -> Iterator[PytestCommand]:
    """Yield every pytest invocation in a shell block, with its gate state."""
    for logical in _logical_commands(block):
        for segment in _split_shell_segments(logical):
            tokens = _tokens(segment)
            if not tokens or tokens[0] in _SHELL_NON_EXECUTORS:
                continue
            if _PYTEST not in tokens:
                continue
            yield PytestCommand(
                location=location,
                text=logical,
                has_escalation=_has_escalation(tokens),
                has_disable_warnings=DISABLE_WARNINGS in tokens,
                disables_warnings_plugin=_disables_warnings_plugin(tokens),
            )


def _walk_yaml(node: object, key: str, path: str) -> Iterator[tuple[str, str]]:
    """Yield ``(dotted-path, value)`` for every mapping key named *key*."""
    if isinstance(node, dict):
        for name, value in node.items():
            here = f"{path}.{name}" if path else str(name)
            if name == key and isinstance(value, str):
                yield here, value
            else:
                yield from _walk_yaml(value, key, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_yaml(item, key, f"{path}[{index}]")


def _yaml_blocks(path: Path, key: str) -> Iterator[tuple[str, str]]:
    """Every string value under *key* in a YAML document, with its path."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    yield from _walk_yaml(document, key, "")


def _relative(path: Path) -> str:
    """A stable POSIX path for a location string, cross-platform."""
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        # A synthetic fixture outside the repo keeps its own path.
        return path.as_posix()


def _ci_yaml_files() -> list[Path]:
    """Workflow files plus composite-action definitions."""
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    if ACTIONS_DIR.exists():
        files += sorted(ACTIONS_DIR.rglob("action.yml")) + sorted(ACTIONS_DIR.rglob("action.yaml"))
    return files


def _yaml_run_pytest_commands(path: Path) -> list[PytestCommand]:
    """All pytest invocations in a YAML file's ``run:`` values."""
    commands: list[PytestCommand] = []
    for yaml_path, block in _yaml_blocks(path, "run"):
        commands.extend(_shell_pytest_commands(f"{_relative(path)}:{yaml_path}", block))
    return commands


def _ci_yaml_pytest_commands() -> list[PytestCommand]:
    """All pytest invocations in workflows and composite actions."""
    commands: list[PytestCommand] = []
    for path in _ci_yaml_files():
        commands.extend(_yaml_run_pytest_commands(path))
    return commands


def _shell_script_pytest_commands() -> list[PytestCommand]:
    """All pytest invocations in the repo's CI shell scripts."""
    commands: list[PytestCommand] = []
    for directory in (CI_SCRIPTS_DIR, SCRIPTS_DIR):
        for path in sorted(directory.rglob("*.sh")):
            commands.extend(_shell_pytest_commands(_relative(path), path.read_text(encoding="utf-8")))
    return commands


def _pre_commit_pytest_commands() -> list[PytestCommand]:
    """All pytest invocations in pre-commit hook entries."""
    commands: list[PytestCommand] = []
    for yaml_path, entry in _yaml_blocks(PRE_COMMIT_CONFIG, "entry"):
        commands.extend(_shell_pytest_commands(f".pre-commit-config.yaml:{yaml_path}", entry))
    return commands


def _constant_sequences(tree: ast.Module) -> dict[str, list[str]]:
    """Simple module-level ``NAME = ["a", "b"]`` assignments, for ``*NAME``."""
    sequences: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        constants = [
            element.value
            for element in value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        for target in targets:
            if isinstance(target, ast.Name):
                sequences[target.id] = constants
    return sequences


def _resolved_constants(node: ast.List | ast.Tuple, sequences: dict[str, list[str]]) -> list[str]:
    """The string constants of an argv list, expanding ``*SHARED_FLAGS``."""
    values: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            values.append(element.value)
        elif isinstance(element, ast.Starred) and isinstance(element.value, ast.Name):
            values.extend(sequences.get(element.value.id, []))
    return values


def _python_argv_pytest_commands(path: Path) -> list[PytestCommand]:
    """Every list/tuple argv in a Python file that contains the pytest command."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sequences = _constant_sequences(tree)
    commands: list[PytestCommand] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        constants = _resolved_constants(node, sequences)
        if _PYTEST not in constants:
            continue
        commands.append(
            PytestCommand(
                location=f"{_relative(path)}:{node.lineno}",
                text=" ".join(constants),
                has_escalation=_has_escalation(constants),
                has_disable_warnings=DISABLE_WARNINGS in constants,
                disables_warnings_plugin=_disables_warnings_plugin(constants),
            )
        )
    return commands


def _python_pytest_commands() -> list[PytestCommand]:
    """All pytest argv lists in the repo's CI and developer scripts."""
    commands: list[PytestCommand] = []
    for directory in (CI_SCRIPTS_DIR, SCRIPTS_DIR):
        for path in sorted(directory.rglob("*.py")):
            commands.extend(_python_argv_pytest_commands(path))
    return commands


def _offenders(commands: list[PytestCommand]) -> list[PytestCommand]:
    """Commands that lost the gate and are not deliberately exempt."""
    return [command for command in commands if not command.ok and not command.location.startswith(tuple(EXEMPTIONS))]


def _report(offenders: list[PytestCommand]) -> str:
    """One failure message naming every offending command."""
    return "pytest commands must carry the unraisable-warning gate:\n\n" + "\n\n".join(
        offender.problem() for offender in offenders
    )


def test_every_ci_yaml_pytest_command_carries_the_warning_gate() -> None:
    """No workflow or composite-action ``run:`` may invoke pytest without the gate."""
    commands = _ci_yaml_pytest_commands()
    assert len(commands) >= 15, f"expected the CI pytest command set, found {len(commands)}"
    sources = {command.location.split(":")[0] for command in commands}
    assert ".github/workflows/bdd.yml" in sources
    assert ".github/workflows/merge-queue.yml" in sources
    offenders = _offenders(commands)
    assert not offenders, _report(offenders)


def test_every_ci_shell_script_pytest_command_carries_the_warning_gate() -> None:
    """The coverage shards' pytest invocation keeps the gate."""
    commands = _shell_script_pytest_commands()
    sources = {command.location for command in commands}
    assert ".github/scripts/coverage-thresholds.sh" in sources
    offenders = _offenders(commands)
    assert not offenders, _report(offenders)


def test_every_python_pytest_argv_carries_the_warning_gate() -> None:
    """Every script that builds a pytest argv list keeps the gate."""
    commands = _python_pytest_commands()
    sources = {command.location.split(":")[0] for command in commands}
    assert "scripts/run_test_suite_quality.py" in sources
    assert "scripts/run_affected_tests.py" in sources
    assert "scripts/run_changed_tests.py" in sources
    offenders = _offenders(commands)
    assert not offenders, _report(offenders)


def test_every_pre_commit_pytest_entry_carries_the_warning_gate() -> None:
    """A pre-commit hook that runs pytest keeps the gate too."""
    commands = _pre_commit_pytest_commands()
    assert commands, "expected the product-map consistency hook to invoke pytest"
    offenders = _offenders(commands)
    assert not offenders, _report(offenders)


def test_exemption_list_is_empty() -> None:
    """No current command needs an exemption; adding one is a reviewed decision."""
    assert not EXEMPTIONS


def test_parser_joins_continuations_and_ignores_prose_and_comments() -> None:
    """A continuation-joined command is one invocation; echo/comment text is not."""
    block = (
        "# pytest tests/unit/ (a comment, not a command)\n"
        'echo "Running: pytest tests/unit/"\n'
        "printf 'pytest %s\\n' tests/unit/\n"
        "uv run pytest tests/unit/ -q \\\n"
        f'  -W "{ESCALATION}" {DISABLE_WARNINGS}\n'
    )
    commands = list(_shell_pytest_commands("synthetic:run", block))
    assert len(commands) == 1
    assert commands[0].ok
    assert "tests/unit/" in commands[0].text


def test_parser_flags_a_command_that_dropped_the_escalation() -> None:
    """Removing the ``-W`` flag is caught and named."""
    commands = list(_shell_pytest_commands("synthetic:run", f"uv run pytest tests/unit/ -q {DISABLE_WARNINGS}\n"))
    assert len(commands) == 1
    assert not commands[0].ok
    assert "-W" in commands[0].problem()


def test_parser_flags_a_disabled_warnings_plugin() -> None:
    """``-p no:warnings`` is an offence even alongside the ``-W`` flag."""
    commands = list(
        _shell_pytest_commands(
            "synthetic:run",
            f'uv run pytest tests/unit/ -q -p no:warnings -W "{ESCALATION}" {DISABLE_WARNINGS}\n',
        )
    )
    assert len(commands) == 1
    assert commands[0].disables_warnings_plugin
    assert not commands[0].ok


def test_parser_does_not_accept_a_flag_that_is_only_in_a_comment() -> None:
    """A trailing comment naming the flag does not satisfy the gate."""
    commands = list(
        _shell_pytest_commands(
            "synthetic:run",
            f"uv run pytest tests/unit/ -q  # -W {ESCALATION} {DISABLE_WARNINGS}\n",
        )
    )
    assert len(commands) == 1
    assert not commands[0].ok


def test_yaml_scan_handles_block_scalars_and_continuations(tmp_path: Path) -> None:
    """A ``run: |`` block with a continuation is parsed like the real workflows."""
    workflow = tmp_path / "synthetic.yml"
    workflow.write_text(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - name: run\n"
        "        run: |\n"
        "          uv run pytest tests/unit/ -q \\\n"
        f"            {DISABLE_WARNINGS}\n",
        encoding="utf-8",
    )
    commands = _yaml_run_pytest_commands(workflow)
    assert len(commands) == 1
    assert not commands[0].ok


def test_python_scan_resolves_a_starred_shared_flag_tuple(tmp_path: Path) -> None:
    """A refactor into ``SHARED_FLAGS`` keeps the gate detectable."""
    script = tmp_path / "runner.py"
    script.write_text(
        "SHARED_FLAGS = ['-W', 'error::pytest.PytestUnraisableExceptionWarning', '--disable-warnings']\n"
        "cmd = ['uv', 'run', 'pytest', 'tests/unit/', *SHARED_FLAGS]\n",
        encoding="utf-8",
    )
    commands = _python_argv_pytest_commands(script)
    assert len(commands) == 1
    assert commands[0].ok
