"""Unit tests for the incomplete-scan detection in scripts/run_semgrep.py.

Semgrep exits **0** when a per-rule match times out on a file: it SKIPS that
file and prints only ``Warning: N timeout error(s) ...``. A gate that reads
the exit code alone therefore reports green on an incomplete scan — the exact
silent-decay class FAR-1859 exists to close.

The wrapper detects this two ways: primarily from the structured
``--json-output`` payload (an ``errors`` entry of ``type == "Timeout"``), and
secondarily from ANCHORED text signatures. These tests pin both detectors,
including the mislabel the anchoring prevents: an unanchored ``"timeout
error"`` substring matches a FINDING that merely quotes the phrase
(``pipelines.py`` contains the literal ``dispatch.wait_timeout error``), which
would report a clean scan as incomplete. The final test asserts the old bare
substring *would* have mislabelled that text — so this suite fails if the
detector is reverted to it (prove-the-fix).

The suite also pins the WINDOWS-path contract at the `_run_windows` level, so
the two conditions can never be swapped silently:

- a REPORTED per-rule timeout (JSON ``Timeout`` payload, and the anchored text
  signature) — including through the real ``_run_windows`` code path — returns
  non-zero;
- a WALL-CLOCK KILL (``subprocess.TimeoutExpired``) returns **0** with a loud
  warning ONLY when the killed process's captured output holds no per-rule skip —
  it is then a machine-load / changed-file-count condition, not a detected skip,
  and CI (Linux) is the authoritative full scan. A kill that follows a REPORTED
  skip (the skip is present in the captured ``exc.stdout``/``exc.stderr``)
  returns **1** instead (FAR-1933 T4);
- a genuine tool error (missing binary, spawn failure) returns 0 with a warning.

The module is loaded from the repo-root ``scripts/`` directory (not a package)
via ``SourceFileLoader``, matching the sibling ``tests/unit/scripts/`` suite
(e.g. ``test_run_validate_semgrep_rules.py``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

import pytest
import yaml

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_semgrep.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_semgrep.py)")

_loader = SourceFileLoader("run_semgrep", str(script_path))
mod = module_from_spec(spec_from_loader("run_semgrep", _loader))
_loader.exec_module(mod)

#: Repo root, for the FAR-1933 T4 CI-workflow coherence test below.
_REPO_ROOT = script_path.parent.parent

#: The two real skip signatures, verbatim from the pinned semgrep's output.
_TIMEOUT_WARNING = (
    "Warning: 1 timeout error(s) in backend/src/modulo/api/mcp_server.py when running the following rules: "
    "[semgrep.split-on-db-url]\n"
)
_TIMEOUT_STOPPED = (
    "Semgrep stopped running rules on backend/src/modulo/api/mcp_server.py after 1 timeout error(s). "
    "See `--timeout-threshold` for more info.\n"
)
#: A FINDING excerpt that quotes the phrase: must NOT be read as a skip.
_FINDING_QUOTING_PHRASE = (
    "backend/src/modulo/api/routes/pipelines.py:1617: dispatch.wait_timeout error not surfaced to the caller\n"
)
#: The ONE non-fatal parse skip that reproduces per-file on the invocation CI
#: uses (`uv run --project backend --no-sync semgrep`): the PEP 695 generic
#: ``async def _run_db_shell[**TOOL_SHELL_P](`` at mcp_server.py:2111, which
#: costs exactly one line. Observed 2026-10-18 — ``--json-output`` carries a
#: ``PartialParsing`` error naming that line, and ``--verbose`` prints
#: "[WARN] Syntax error at line ...:2111" plus "… (1 lines skipped)". The same
#: per-file scan of node_runner.py reports ``errors: []`` and "• <none>", so no
#: parse-skip attribution for that file is reproducible here. A line like this
#: must never be read as a per-rule timeout skip.
_PARTIAL_PARSING = (
    "[WARN] Syntax error at line backend/src/modulo/api/mcp_server.py:2111:\n"
    " `[**TOOL_SHELL_P]` was unexpected\n"
    "\n"
    "  Partially analyzed due to parsing or internal Semgrep errors\n"
    "\n"
    "   • backend/src/modulo/api/mcp_server.py (1 lines skipped)\n"
)

#: Real ``errors[]`` entries captured from the pinned semgrep's ``--json-output``
#: (2026-10-18, against ``backend/src/modulo/api/mcp_server.py``). They are
#: verbatim because the two tags have DIFFERENT wire types: a ``Timeout`` tag is
#: a plain string, but a ``PartialParsing`` tag is a **list** whose first element
#: is the type name. Testing that list against a ``frozenset`` raised
#: ``TypeError: unhashable type: 'list'`` and fail-opened the gate — the defect
#: FAR-1933 closes — so both shapes are pinned here as they actually arrive.
_TIMEOUT_ERROR_ENTRY: dict[str, object] = {
    "code": 2,
    "level": "warn",
    "message": "Timeout when running semgrep.split-on-db-url on backend/src/modulo/api/mcp_server.py:\n ",
    "path": "backend/src/modulo/api/mcp_server.py",
    "rule_id": "semgrep.split-on-db-url",
    "type": "Timeout",
}
_PARTIAL_PARSING_ERROR_ENTRY: dict[str, object] = {
    "code": 3,
    "level": "warn",
    "message": ("Syntax error at line backend/src/modulo/api/mcp_server.py:2111:\n `[**TOOL_SHELL_P]` was unexpected"),
    "path": "backend/src/modulo/api/mcp_server.py",
    "spans": [
        {
            "end": {"col": 40, "line": 2111, "offset": 16},
            "file": "backend/src/modulo/api/mcp_server.py",
            "start": {"col": 24, "line": 2111, "offset": 0},
        }
    ],
    "type": ["PartialParsing", [{"end": {"line": 2111}, "start": {"line": 2111}}]],
}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Secondary net — anchored text signatures
# ---------------------------------------------------------------------------


def test_text_detector_matches_real_timeout_warning() -> None:
    assert mod._semgrep_skipped_files(_TIMEOUT_WARNING)


def test_text_detector_matches_real_stopped_running_rules_line() -> None:
    assert mod._semgrep_skipped_files(_TIMEOUT_STOPPED)


def test_text_detector_ignores_clean_output() -> None:
    clean = "Scan completed successfully.\nFindings: 0 (0 blocking)\nRan 47 rules on 1 file: 0 findings.\n"
    assert not mod._semgrep_skipped_files(clean)


def test_anchored_detector_does_not_match_a_finding_quoting_the_phrase() -> None:
    """The phrase inside a finding excerpt is not a skip — anchoring prevents it."""
    assert not mod._semgrep_skipped_files(_FINDING_QUOTING_PHRASE)


def test_anchored_detector_does_not_match_partially_analyzed() -> None:
    assert not mod._semgrep_skipped_files(_PARTIAL_PARSING)


# ---------------------------------------------------------------------------
# Primary detector — structured --json-output errors
# ---------------------------------------------------------------------------


def test_json_detector_true_for_string_shaped_timeout_error(tmp_path: Path) -> None:
    """A real ``Timeout`` tag is a plain string — the original, still-true shape."""
    path = tmp_path / "timeout.json"
    _write_json(path, {"errors": [_TIMEOUT_ERROR_ENTRY], "results": []})
    assert mod._semgrep_json_timeout(path)


def test_json_detector_true_for_list_shaped_timeout_error(tmp_path: Path) -> None:
    """The primary detector must fire on a LIST-shaped tag too.

    Semgrep serialises some tags as ``[<name>, <spans>]``; a detector that tests
    the raw tag membership-raises on it (`TypeError: unhashable type: 'list'`).
    This pins the normalisation so the real wire shape is caught.
    """
    payload = {"errors": [{**_TIMEOUT_ERROR_ENTRY, "type": ["Timeout", [{"line": 1}]]}], "results": []}
    path = tmp_path / "timeout-list.json"
    _write_json(path, payload)
    assert mod._semgrep_json_timeout(path)


def test_json_detector_false_for_clean_payload(tmp_path: Path) -> None:
    path = tmp_path / "clean.json"
    _write_json(path, {"errors": [], "results": []})
    assert not mod._semgrep_json_timeout(path)


def test_json_detector_false_for_partial_parsing_only(tmp_path: Path) -> None:
    """The REAL PartialParsing shape is expected and non-fatal.

    Its tag is a list, not a string (see ``_PARTIAL_PARSING_ERROR_ENTRY``), and
    it must neither fail the gate nor make the detector raise.
    """
    path = tmp_path / "partial.json"
    _write_json(path, {"errors": [_PARTIAL_PARSING_ERROR_ENTRY], "results": []})
    assert not mod._semgrep_json_timeout(path)


def test_json_detector_false_for_list_typed_partial_parsing(tmp_path: Path) -> None:
    """The pinned semgrep emits PartialParsing's ``type`` as ``[name, spans]``.

    Unlike the sibling string-shaped case, semgrep **1.79.0** — the version
    pinned in ``backend/uv.lock``, and the one
    ``uv run --project backend --no-sync semgrep`` actually resolves to —
    serialises the non-fatal PEP 695 parse skip with a LIST-typed ``type``.
    (The PATH uv-tool semgrep 1.175.0 emits no error at all for the same file,
    so it is the lockfile version this test pins.)

    The trigger is file-conditional, not timeout-conditional: the list-typed
    entry is emitted on EVERY scan of a source file carrying PEP 695 generics
    (at least nine modules, including ``api/mcp_server.py``,
    ``api/routes/runs.py``, ``auth/dependencies.py``, ``core/team_visibility.py``,
    ``db/soft_delete.py`` and ``db/crud/base.py``), whether or not any rule
    timed out. Only files changed since the baseline are scanned
    (``--baseline-commit=HEAD`` on Linux; the explicitly passed changed files on
    Windows), so a commit touching no such file does not crash — but any commit
    touching one does. The detector must treat it as non-fatal rather than
    raising ``TypeError: unhashable type: 'list'``, which aborted the
    pre-commit hook before it could report a result.
    """
    path = tmp_path / "partial-list.json"
    _write_json(
        path,
        {
            "errors": [
                {
                    "type": [
                        "PartialParsing",
                        [
                            {
                                "path": "backend/src/modulo/api/mcp_server.py",
                                "start": {"line": 2111, "col": 24, "offset": 0},
                                "end": {"line": 2111, "col": 40, "offset": 16},
                            }
                        ],
                    ],
                    "message": (
                        "Syntax error at line backend/src/modulo/api/mcp_server.py:2111: "
                        "`[**TOOL_SHELL_P]` was unexpected"
                    ),
                }
            ],
            "results": [],
        },
    )
    assert not mod._semgrep_json_timeout(path)


def test_json_detector_true_for_list_typed_timeout(tmp_path: Path) -> None:
    """A fatal class inside a list-typed ``type`` is still fatal."""
    path = tmp_path / "timeout-list.json"
    _write_json(
        path,
        {"errors": [{"type": ["PartialParsing", "Timeout"], "message": "timeout"}], "results": []},
    )
    assert mod._semgrep_json_timeout(path)


def test_json_detector_never_raises_on_unexpected_error_shapes(tmp_path: Path) -> None:
    """T1: the detector must NEVER raise, whatever an ``errors[]`` entry holds."""
    unexpected: list[object] = [
        {},
        {"type": None},
        {"type": []},
        {"type": {}},
        {"type": 42},
        {"type": ["PartialParsing", []]},
        {"type": [["nested"], "x"]},
        "not-a-dict",
        42,
        None,
    ]
    for index, entry in enumerate(unexpected):
        path = tmp_path / f"shape-{index}.json"
        _write_json(path, {"errors": [entry], "results": []})
        assert isinstance(mod._semgrep_json_timeout(path), bool)


def test_json_detector_false_when_file_missing(tmp_path: Path) -> None:
    assert not mod._semgrep_json_timeout(tmp_path / "does-not-exist.json")


def test_json_detector_false_when_payload_malformed(tmp_path: Path) -> None:
    path = tmp_path / "malformed.json"
    path.write_text("not json{", encoding="utf-8")
    assert not mod._semgrep_json_timeout(path)


# ---------------------------------------------------------------------------
# Combined detector — JSON primary, text net
# ---------------------------------------------------------------------------


def test_combined_detector_true_from_json_even_with_clean_text(tmp_path: Path) -> None:
    path = tmp_path / "timeout.json"
    _write_json(path, {"errors": [{"type": "Timeout"}], "results": []})
    assert mod._semgrep_scan_incomplete("Scan completed successfully.\n", path)


def test_combined_detector_true_from_text_when_json_absent(tmp_path: Path) -> None:
    assert mod._semgrep_scan_incomplete(_TIMEOUT_WARNING, tmp_path / "missing.json")


def test_combined_detector_false_on_clean_scan(tmp_path: Path) -> None:
    path = tmp_path / "clean.json"
    _write_json(path, {"errors": [], "results": []})
    assert not mod._semgrep_scan_incomplete("Ran 47 rules on 1 file: 0 findings.\n", path)


# ---------------------------------------------------------------------------
# Prove-the-fix: the anchored detector is what prevents the mislabel
# ---------------------------------------------------------------------------


def _legacy_skipped_files(output: str) -> bool:
    """The pre-FAR-1859 detector: an unanchored, lowercased substring test."""
    lowered = output.lower()
    return "timeout error" in lowered or "stopped running rules" in lowered


def test_anchoring_prevents_the_legacy_mislabel() -> None:
    """Prove-the-fix: the legacy substring detector mislabels a finding that
    merely quotes the phrase, while the anchored detector does not. If the
    detector is reverted to the legacy form, the second assertion fails."""
    assert _legacy_skipped_files(_FINDING_QUOTING_PHRASE)
    assert not mod._semgrep_skipped_files(_FINDING_QUOTING_PHRASE)


# ---------------------------------------------------------------------------
# Windows path — the wall-clock kill fails OPEN, a REPORTED skip fails CLOSED
# ---------------------------------------------------------------------------
#
# ``_run_windows`` is platform-independent code (only ``main`` gates on
# ``sys.platform``), so it is exercised directly here. Each case fakes the git
# change-detection call and the semgrep ``subprocess.run`` so the branch under
# test runs without invoking a real scan.

_CHANGED = ["backend/src/modulo/api/mcp_server.py"]


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["semgrep"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_windows_wall_clock_kill_warns_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """A process kill is environmental: exit 0, but LOUDLY not silently."""
    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(
            mod.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="semgrep", timeout=mod._SEMGREP_TIMEOUT),
        ),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 0
    assert "did NOT complete on Windows" in captured.err
    assert "No per-rule timeout skip was found in the output semgrep emitted before" in captured.err
    assert "CI (Linux)" in captured.err
    # It must be a warning, never reported as a hard FAILURE.
    assert "reported as a FAILURE" not in captured.err


def test_run_windows_wall_clock_kill_with_captured_skip_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A kill AFTER semgrep reported a per-rule skip fails CLOSED.

    ``subprocess.run`` captures what the killed process emitted before the kill,
    so the skip is OBSERVED from that captured output rather than assumed absent
    (FAR-1933 T4): a kill that follows a reported skip must not reopen the
    silent-pass hole FAR-1859 closed.
    """
    exc = subprocess.TimeoutExpired(
        cmd="semgrep",
        timeout=mod._SEMGREP_TIMEOUT,
        output=_TIMEOUT_WARNING,
    )
    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(mod.subprocess, "run", side_effect=exc),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 1
    assert "reported as a FAILURE" in captured.err


def test_run_windows_wall_clock_kill_with_captured_skip_as_bytes_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A captured skip delivered as ``bytes`` still fails CLOSED, without crashing.

    ``subprocess.TimeoutExpired.stdout``/``.stderr`` are typed ``bytes | str |
    None`` — ``bytes`` is the shape when the child was not run with ``text=True``.
    The captured streams are normalised with ``_as_text`` before the detector
    runs, so a byte-typed capture is matched, not silently discarded (FAR-1985).
    """
    exc = subprocess.TimeoutExpired(
        cmd="semgrep",
        timeout=mod._SEMGREP_TIMEOUT,
        output=_TIMEOUT_WARNING.encode("utf-8"),
        stderr=b"",
    )
    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(mod.subprocess, "run", side_effect=exc),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 1
    assert "reported as a FAILURE" in captured.err


def test_as_text_normalises_bytes_str_and_none() -> None:
    """``_as_text`` maps ``None``->``""``, decodes ``bytes``, passes ``str`` through.

    The ``bytes`` decode uses ``errors="replace"`` so a non-UTF-8 capture cannot
    raise while the gate is reporting a failure — the helper must never crash.
    """
    assert isinstance(mod._as_text(None), str)
    assert not mod._as_text(None)
    assert mod._as_text("already text") == "already text"
    assert mod._as_text(b"decoded") == "decoded"
    # Invalid UTF-8 must not raise, and the ASCII remainder survives.
    assert "semgrep" in mod._as_text(b"\xff\xfe semgrep timeout")


def test_run_windows_wall_clock_kill_with_captured_non_skip_stays_open(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A kill whose captured output holds only a FINDING excerpt (no skip) stays open.

    The captured output is scanned with the same anchored detector, so a finding
    that merely quotes the timeout phrase cannot turn an environmental kill into
    a false FAILURE.
    """
    exc = subprocess.TimeoutExpired(
        cmd="semgrep",
        timeout=mod._SEMGREP_TIMEOUT,
        output=_FINDING_QUOTING_PHRASE,
    )
    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(mod.subprocess, "run", side_effect=exc),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 0
    assert "reported as a FAILURE" not in captured.err


def test_run_windows_reported_text_timeout_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """A REPORTED per-rule timeout still fails through the real Windows path."""
    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(mod.subprocess, "run", return_value=_completed(0, stdout=_TIMEOUT_WARNING)),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 1
    assert "reported as a FAILURE" in captured.err


def test_run_windows_reported_json_timeout_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """The JSON primary detector also fails through the real Windows path."""
    captured_json: list[str] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        json_arg = next(arg for arg in cmd if arg.startswith("--json-output="))
        json_path = json_arg.split("=", 1)[1]
        Path(json_path).write_text(json.dumps({"errors": [{"type": "Timeout"}], "results": []}), encoding="utf-8")
        captured_json.append(json_path)
        return _completed(0, stdout="Scan completed successfully.\n")

    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(mod.subprocess, "run", side_effect=_fake_run),
    ):
        rc = mod._run_windows()
    exit_output = capsys.readouterr()
    assert captured_json
    assert rc == 1
    assert "reported as a FAILURE" in exit_output.err


def test_run_windows_mixed_real_payload_with_list_partial_parsing_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The REAL mixed payload must fail CLOSED through the real Windows path.

    Before the fix, the list-shaped ``PartialParsing`` tag made the JSON
    detector raise ``TypeError: unhashable type: 'list'``; the generic
    ``except Exception`` in ``_run_windows`` swallowed it and returned 0 — a
    reported timeout silently passing (FAR-1933 T3). This reproduces the real
    mixed payload (list-shaped PartialParsing for ``mcp_server.py`` PLUS a real
    string ``Timeout`` PLUS the real anchored warning text) end-to-end.
    """
    mixed = {"errors": [_PARTIAL_PARSING_ERROR_ENTRY, _TIMEOUT_ERROR_ENTRY], "results": []}

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        json_arg = next(arg for arg in cmd if arg.startswith("--json-output="))
        Path(json_arg.split("=", 1)[1]).write_text(json.dumps(mixed), encoding="utf-8")
        return _completed(0, stdout="", stderr=_TIMEOUT_WARNING)

    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(mod.subprocess, "run", side_effect=_fake_run),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 1
    assert "reported as a FAILURE" in captured.err


def test_run_windows_spawn_failure_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(
            mod.subprocess, "run", return_value=_completed(1, stderr="Failed to spawn: program not found")
        ),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 0
    assert "WARNING" in captured.err


def test_run_windows_missing_binary_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        mock.patch.object(mod, "_get_changed_py_files", return_value=(_CHANGED, None)),
        mock.patch.object(mod.subprocess, "run", side_effect=FileNotFoundError("uv")),
    ):
        rc = mod._run_windows()
    captured = capsys.readouterr()
    assert rc == 0
    assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# FAR-1933 T4 — the CI `pairs=` block must equal the fixtures that exist
# ---------------------------------------------------------------------------
#
# The `pairs="..."` block in `.github/workflows/ci.yml` is HAND-MAINTAINED, so a
# fixture added on disk without a pair silently goes unenforced — a gap that has
# already cost two review rounds. It is closed structurally here: the pair set
# is re-derived from the repo (discover every file carrying a `# ruleid:` /
# `# ok:` annotation, resolve each rule id to the rule FILE that declares it)
# and compared for exact equality with the block.

#: A real annotation is a whole comment line `# ruleid: <id>` / `# ok: <id>`
#: (optionally indented), with a rule id of word/hyphen characters. Anchoring
#: the whole line keeps PROSE that merely mentions the syntax out of the sweep.
_ANNOTATION = re.compile(r"^\s*#\s*(?:ruleid|ok):\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*$", re.MULTILINE)
#: Directories never worth descending into: installed deps, VCS metadata, caches,
#: and ``.agents`` — a mount holding untracked worktree copies of this tree.
_SKIP_DIRS = frozenset({".venv", "venv", "node_modules", ".git", ".agents", "__pycache__", ".pytest_cache"})
_ANNOTATABLE_SUFFIXES = frozenset({".py", ".ts", ".vue", ".js"})


def _ci_pair_block(ci_path: Path | None = None) -> set[tuple[str, str]]:
    """Parse the ``pairs="..."`` block out of the CI workflow into pairs."""
    source = ci_path or (_REPO_ROOT / ".github/workflows/ci.yml")
    ci_text = source.read_text(encoding="utf-8")
    match = re.search(r'pairs="\n(.*?)\n\s*"', ci_text, re.DOTALL)
    if match is None:
        raise AssertionError(f"could not locate the `pairs=` block in {source}")
    pairs: set[tuple[str, str]] = set()
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        rule, fixture = line.split(":", 1)
        pairs.add((rule, fixture))
    return pairs


def _rule_id_to_file() -> dict[str, str]:
    """Map every declared semgrep rule id to the rule file that declares it."""
    mapping: dict[str, str] = {}
    for rule_file in sorted((_REPO_ROOT / ".semgrep").rglob("*.yml")):
        if _SKIP_DIRS.intersection(rule_file.relative_to(_REPO_ROOT).parts):
            continue
        document = yaml.safe_load(rule_file.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        for rule in document.get("rules") or []:
            if isinstance(rule, dict) and "id" in rule:
                mapping[rule["id"]] = rule_file.relative_to(_REPO_ROOT).as_posix()
    return mapping


def _annotated_fixtures() -> dict[str, list[str]]:
    """Discover every repo file carrying a semgrep ``# ruleid:``/``# ok:`` annotation.

    The walk skips untracked mounts that MIRROR the tree — notably
    ``.agents/``, under which every linked worktree holds its own copy of
    ``.semgrep/tests/*.py`` — so a fixture cannot be double-counted (relative to
    the repo root) and make the pair sets disagree.
    """
    found: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for filename in filenames:
            if Path(filename).suffix not in _ANNOTATABLE_SUFFIXES:
                continue
            path = Path(dirpath) / filename
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rule_ids = _ANNOTATION.findall(text)
            if rule_ids:
                found[path.relative_to(_REPO_ROOT).as_posix()] = rule_ids
    return found


def _derived_pairs() -> set[tuple[str, str]]:
    """The (rule file, fixture) pairs implied by the annotations on disk."""
    rule_files = _rule_id_to_file()
    derived: set[tuple[str, str]] = set()
    unresolved: list[str] = []
    for fixture, rule_ids in _annotated_fixtures().items():
        for rule_id in rule_ids:
            rule_file = rule_files.get(rule_id)
            if rule_file is None:
                unresolved.append(f"{fixture} (rule id {rule_id!r})")
            else:
                derived.add((rule_file, fixture))
    if unresolved:
        raise AssertionError(
            "semgrep test annotation(s) could not be resolved to a rule file — add each "
            "pair to the `pairs=` block in .github/workflows/ci.yml: " + "; ".join(sorted(unresolved))
        )
    return derived


def test_ci_semgrep_pairs_match_the_fixtures_on_disk() -> None:
    """Every annotated fixture is paired in CI, and every pair has a fixture."""
    ci_pairs = _ci_pair_block()
    derived = _derived_pairs()
    assert derived == ci_pairs, (
        "the `pairs=` block in .github/workflows/ci.yml and the semgrep fixtures on disk disagree.\n"
        f"  in pairs= but not derived on disk: {sorted(ci_pairs - derived)}\n"
        f"  derived on disk but missing from pairs=: {sorted(derived - ci_pairs)}"
    )
