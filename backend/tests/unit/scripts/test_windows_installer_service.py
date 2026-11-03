"""FAR-2330 - the Windows installer registers the service with a valid image path.

``deploy/windows/modulo.iss`` registers the service from ``[Code]`` with an
``sc.exe create`` call. The launcher argument (``service``) must sit OUTSIDE the
quotes that delimit the executable path. With it inside, sc.exe stores
``ImagePath`` as the literal string ``C:\\Program Files\\Modulo\\bin\\modulo.exe
service`` - a path that does not exist - so the Service Control Manager cannot
start the service, and the installer fails only on the operator's machine.

These tests RECONSTRUCT the generated command from the ``.iss`` source rather
than string-matching a copy of it: the Pascal concatenation expression is read
out of ``RegisterService``, its ``ServiceExe`` operand is bound to the real
default install path, and the resulting command line is evaluated. A change to
the quoting, the operand order or the argument position therefore fails here,
which is what a comment-only assertion could not do.

The same file also pins the two ``[Setup]``/``[UninstallRun]`` details that keep
the installer compiling without warnings: the non-deprecated architecture
identifiers and a ``RunOnceId`` on every uninstall step.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ISS = _REPO_ROOT / "deploy" / "windows" / "modulo.iss"

# The installer's default directory is {autopf}\Modulo, so the service image
# path contains spaces on a stock install. That is exactly the shape that makes
# the quote positions matter.
_SERVICE_EXE = r"C:\Program Files\Modulo\bin\modulo.exe"


def _source() -> str:
    assert _ISS.is_file(), f"the Windows installer source is missing: {_ISS}"
    return _ISS.read_text(encoding="utf-8")


def _section_lines(source: str, section: str) -> list[str]:
    """The raw lines of one ``[Section]``, without the header or any trailing section."""
    header = f"[{section}]"
    start = source.index(header) + len(header)
    lines: list[str] = []
    for line in source[start:].splitlines():
        if line.strip().startswith("["):
            break
        lines.append(line)
    return lines


def _section_entries(source: str, section: str) -> list[str]:
    """The non-blank, non-comment entries of one ``[Section]``."""
    return [
        line.strip() for line in _section_lines(source, section) if line.strip() and not line.strip().startswith(";")
    ]


def _setup_values(source: str, key: str) -> list[str]:
    """Every value assigned to ``key`` in ``[Setup]``, in source order.

    A key appears more than once when the file gates it on the Inno Setup
    version: the first entry is the branch a current compiler takes and the
    second is the fallback for an older one.
    """
    return [entry[len(key) + 1 :].strip() for entry in _section_entries(source, "Setup") if entry.startswith(f"{key}=")]


def _register_service_body(source: str) -> str:
    """The body of the ``RegisterService`` function."""
    start = source.index("function RegisterService")
    end = source.index("\nend;\n", start)
    return source[start:end]


# The ``sc.exe create`` call: its second argument is the command string. The
# expression contains no comma, so the argument ends at the next ``,''`` -
# the empty working directory that follows.
_CREATE_EXEC = re.compile(
    r"Exec\(\s*'sc\.exe'\s*,\s*(?P<expr>'create Modulo .*?')\s*,\s*''",
    re.DOTALL,
)
_SERVICE_EXE_ASSIGNMENT = re.compile(r"^\s*ServiceExe := (?P<expr>.+);\s*$", re.MULTILINE)


def _split_pascal_concat(expression: str) -> list[str]:
    """Split a Pascal string-concatenation expression on its top-level ``+``."""
    parts: list[str] = []
    buffer: list[str] = []
    in_literal = False
    for char in expression:
        if char == "'":
            in_literal = not in_literal
            buffer.append(char)
        elif char == "+" and not in_literal:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer).strip())
    return parts


def _evaluate_concat(expression: str, variables: dict[str, str]) -> str:
    """Evaluate a Pascal concatenation of string literals and named variables."""
    pieces: list[str] = []
    for part in _split_pascal_concat(expression):
        if len(part) >= 2 and part.startswith("'") and part.endswith("'"):
            pieces.append(part[1:-1].replace("''", "'"))
            continue
        assert part in variables, (
            f"unrecognised operand {part!r} in the service command expression; "
            "bind it in this test rather than letting the assertion pass by accident"
        )
        pieces.append(variables[part])
    return "".join(pieces)


def _service_exe(source: str) -> str:
    """Bind the ``ServiceExe`` variable to the real default install path."""
    body = _register_service_body(source)
    match = _SERVICE_EXE_ASSIGNMENT.search(body)
    assert match is not None, "RegisterService no longer assigns ServiceExe"
    assignment = match.group("expr").strip()
    assert assignment == "ExpandConstant('{app}') + '\\bin\\modulo.exe'", (
        f"ServiceExe is no longer the installed launcher: {assignment}"
    )
    return _SERVICE_EXE


def _create_command(source: str) -> str:
    """Reconstruct the exact ``sc.exe create`` argument list from the source."""
    body = _register_service_body(source)
    match = _CREATE_EXEC.search(body)
    assert match is not None, "RegisterService no longer calls Exec('sc.exe', 'create Modulo ...')"
    return _evaluate_concat(match.group("expr"), {"ServiceExe": _service_exe(source)})


def _split_windows_argv(command: str) -> list[str]:
    """Split a Windows command line the way the C runtime does, honouring ``"``.

    This is how sc.exe sees the argument list: a quoted run is one argument and
    the quotes themselves are consumed, so ``binPath= "C:\\a b\\x.exe service"``
    hands sc.exe a single value ``C:\\a b\\x.exe service``.
    """
    argv: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in command:
        if char == '"':
            in_quotes = not in_quotes
        elif char == " " and not in_quotes:
            if current:
                argv.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        argv.append("".join(current))
    return argv


# ---------------------------------------------------------------------------
# The defect: only the executable path is quoted, and `service` follows it.
# ---------------------------------------------------------------------------


def test_create_command_is_exactly_the_expected_string() -> None:
    command = _create_command(_source())
    assert command == f'create Modulo binPath= "{_SERVICE_EXE}" service start= auto'


def test_only_the_executable_is_quoted() -> None:
    command = _create_command(_source())
    assert command.count('"') == 2, f"the command must quote exactly one value: {command}"
    open_quote = command.index('"')
    close_quote = command.index('"', open_quote + 1)
    assert command[open_quote + 1 : close_quote] == _SERVICE_EXE
    assert command[close_quote + 1 :] == " service start= auto"


def test_sc_exe_receives_the_launcher_argument_outside_bin_path() -> None:
    """The value sc.exe stores as ImagePath must be the executable, nothing more."""
    command = _create_command(_source())
    argv = _split_windows_argv(command)
    assert argv[0] == "create"
    assert argv[1] == "Modulo"
    assert "binPath=" in argv
    bin_path_at = argv.index("binPath=")
    assert argv[bin_path_at + 1] == _SERVICE_EXE
    assert argv[bin_path_at + 2] == "service"
    assert argv[-2] == "start="
    assert argv[-1] == "auto"


def test_the_launcher_argument_is_not_inside_the_image_path() -> None:
    """The exact regression: ``"<exe> service"`` names a binary that does not exist."""
    argv = _split_windows_argv(_create_command(_source()))
    image_path = argv[argv.index("binPath=") + 1]
    assert " service" not in image_path
    assert not image_path.endswith("service")


# ---------------------------------------------------------------------------
# The warnings the installer must not emit at compile time.
# ---------------------------------------------------------------------------


def test_architecture_identifiers_are_the_non_deprecated_form() -> None:
    source = _source()
    assert _setup_values(source, "ArchitecturesInstallIn64BitMode") == ["x64compatible", "x64"]
    assert _setup_values(source, "ArchitecturesAllowed") == ["x64compatible", "x64"]


def test_architecture_identifiers_are_gated_on_the_6_3_boundary() -> None:
    """x64compatible does not exist before 6.3, and the release workflow pins 6.2.2."""
    assert "#if VER >= EncodeVer(6,3,0)" in _source()


def test_every_uninstall_run_entry_has_a_distinct_run_once_id() -> None:
    entries = _section_entries(_source(), "UninstallRun")
    identifiers: list[str] = []
    for entry in entries:
        match = re.search(r'RunOnceId:\s*"(?P<identifier>[^"]+)"', entry)
        assert match is not None, f"[UninstallRun] entry without a RunOnceId: {entry}"
        identifiers.append(match.group("identifier"))
    assert identifiers == ["StopModuloService", "DeleteModuloService"]


def test_uninstall_entries_still_stop_then_delete_the_service() -> None:
    """The RunOnceId addition must not reorder or rewrite the uninstall steps."""
    entries = _section_entries(_source(), "UninstallRun")
    assert len(entries) == 2
    assert 'Parameters: "stop Modulo"' in entries[0]
    assert 'Parameters: "delete Modulo"' in entries[1]
