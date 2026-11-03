"""FAR-2335 - the Windows installer stores a valid service image path and does
not abort the install over the deferred Windows service seam.

``deploy/windows/modulo.iss`` registers the service from ``[Code]`` with an
``sc.exe create`` call. two things are pinned here, both of them properties of
the RESULT rather than of the source text:

1. ``ImagePath`` must be stored QUOTED, with the launcher argument inside the
   quotes (``"C:\\Program Files\\Modulo\\modulo.exe" service``). ``sc.exe``
   accepts only ``key= value`` options, so a bare ``service`` token after the
   quoted path is rejected with 1639 (ERROR_INVALID_COMMAND_LINE): sc.exe
   prints its usage block and registers nothing. Measured on an elevated
   Windows console before this fix. The quotes around the executable must be
   escaped as ``\\"`` in the argument sc.exe receives, which is why a
   command-string assertion alone cannot catch the regression - sc.exe consumes
   its own quoting.

2. A failed service START must not abort the installation. The launcher has no
   Windows service mode yet (``TODO(P3)`` in
   ``backend/src/modulo/launcher/entry.py``), so ``modulo.exe service`` cannot
   start on any machine. That is a known limitation to report loudly, not a
   reason to throw away a completed install.

These tests RECONSTRUCT the generated command from the ``.iss`` source rather
than string-matching a copy of it: the Pascal concatenation expression is read
out of ``RegisterService``, its ``ServiceExe`` operand is bound to the real
default install path, and the resulting command line is split the way sc.exe
sees it. A change to the quoting, the operand order or the argument position
therefore fails here. On an elevated Windows host the reconstructed command is
also executed for real and the persisted ``ImagePath`` is read back from the
registry; on any other host that test skips with the reason stated, and the
command-line proof still runs.

The same file also pins the two ``[Setup]``/``[UninstallRun]`` details that keep
the installer compiling without warnings: the non-deprecated architecture
identifiers and a ``RunOnceId`` on every uninstall step.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ISS = _REPO_ROOT / "deploy" / "windows" / "modulo.iss"

# The installer's default directory is {autopf}\Modulo, so the service image
# path contains spaces on a stock install. That is exactly the shape that makes
# the quote positions matter.
_SERVICE_EXE = r"C:\Program Files\Modulo\modulo.exe"

# What the Service Control Manager must have stored for the service to start:
# the executable quoted (so the space in "Program Files" is unambiguous),
# followed by the launcher argument.
_EXPECTED_IMAGE_PATH = f'"{_SERVICE_EXE}" service'


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


def _procedure_body(source: str, header: str) -> str:
    """The body of a procedure or function, up to its column-0 ``end;``.

    Nested blocks are indented, so an unindented ``end;`` can only terminate the
    declaration itself.
    """
    start = source.index(header)
    end = source.index("\nend;\n", start)
    return source[start:end]


def _register_service_body(source: str) -> str:
    """The body of the ``RegisterService`` function."""
    return _procedure_body(source, "function RegisterService")


# The ``sc.exe create`` call: its second argument is the command string. The
# expression contains no comma, so the argument ends at the next ``,''`` - the
# empty working directory that follows.
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
    assert assignment == "ExpandConstant('{app}') + '\\modulo.exe'", (
        f"ServiceExe is no longer the installed launcher: {assignment}"
    )
    return _SERVICE_EXE


def _create_command(source: str) -> str:
    """Reconstruct the exact ``sc.exe create`` argument list from the source."""
    body = _register_service_body(source)
    match = _CREATE_EXEC.search(body)
    assert match is not None, "RegisterService no longer calls Exec('sc.exe', 'create Modulo ...')"
    return _evaluate_concat(match.group("expr"), {"ServiceExe": _service_exe(source)})


def _sc_argv(command: str) -> list[str]:
    """Split a command line the way sc.exe sees it, honouring escaped quotes.

    sc.exe is handed one argument string here (Inno's ``Exec`` does not
    re-quote it), and it splits on whitespace outside quotes. A ``\\"`` inside a
    quoted run is an escaped literal quote rather than a delimiter, which is how
    the executable's own quotes survive into the stored ``ImagePath``. A plain
    quote toggles quoting and is itself consumed - that consumption is what made
    an assertion on the command string alone unable to see the defect.
    """
    argv: list[str] = []
    current: list[str] = []
    in_quotes = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and index + 1 < len(command) and command[index + 1] == '"':
            current.append('"')
            index += 2
            continue
        if char == '"':
            in_quotes = not in_quotes
        elif char == " " and not in_quotes:
            if current:
                argv.append("".join(current))
                current = []
        else:
            current.append(char)
        index += 1
    if current:
        argv.append("".join(current))
    return argv


def _bin_path_value(command: str) -> str:
    """The value sc.exe will store as ``ImagePath``."""
    argv = _sc_argv(command)
    return argv[argv.index("binPath=") + 1]


# ---------------------------------------------------------------------------
# The image path sc.exe is asked to persist.
# ---------------------------------------------------------------------------


def test_create_command_is_exactly_the_expected_string() -> None:
    command = _create_command(_source())
    assert command == f'create Modulo binPath= "\\"{_SERVICE_EXE}\\" service" start= auto'


def test_sc_exe_receives_no_positional_launcher_token() -> None:
    """``create``/``Modulo`` are the only positional tokens; ``service`` is data.

    A bare ``service`` after the quoted path is the exact defect this test
    guards: sc.exe rejects it with 1639 and registers nothing.
    """
    argv = _sc_argv(_create_command(_source()))
    assert argv == ["create", "Modulo", "binPath=", _EXPECTED_IMAGE_PATH, "start=", "auto"]


def test_the_persisted_image_path_is_quoted_with_the_launcher_argument_inside() -> None:
    assert _bin_path_value(_create_command(_source())) == _EXPECTED_IMAGE_PATH


def test_the_executable_path_keeps_its_quotes() -> None:
    """The regression: an unquoted path is a local-privilege-escalation shape."""
    value = _bin_path_value(_create_command(_source()))
    assert value.startswith('"')
    assert value.index('"', 1) == len(_SERVICE_EXE) + 1
    assert value[1 : len(_SERVICE_EXE) + 1] == _SERVICE_EXE


# ---------------------------------------------------------------------------
# A deferred service START must be reported loudly and must not abort.
# ---------------------------------------------------------------------------


def test_register_service_reports_creation_separately_from_start() -> None:
    body = _register_service_body(_source())
    assert "function RegisterService(var ServiceStarted: Boolean): Boolean;" in body
    # The creation result is committed before the start is attempted, so a
    # start failure can never be reported as a registration failure.
    assert body.index("Result := True;") < body.index("service registration (start)")


def test_the_only_abort_is_the_service_creation_failure() -> None:
    source = _source()
    code = source[source.index("[Code]") :]
    assert code.count("Abort;") == 1, "a second Abort would re-introduce a fatal path"
    body = _procedure_body(source, "procedure CurStepChanged")
    abort_at = body.index("Abort;")
    branch_start = body.index("if not RegisterService(ServiceStarted) then")
    branch_end = body.index("\n    end;", branch_start)
    assert branch_start < abort_at < branch_end


def test_a_service_start_failure_is_handled_without_aborting() -> None:
    body = _procedure_body(_source(), "procedure CurStepChanged")
    deferred_start = body.index("if not ServiceStarted then")
    assert "ReportServiceModeDeferred;" in body[deferred_start:]
    assert "Abort" not in body[deferred_start:]


def test_deferred_service_mode_is_reported_loudly() -> None:
    """The limitation must be visible: stage, log, failure record, support block."""
    body = _procedure_body(_source(), "procedure ReportServiceModeDeferred")
    assert "SetStage(" in body
    assert "WriteInstallLog(" in body
    assert "MarkFailure(" in body
    assert "PrintSupportBlock" in body
    assert "Start Menu shortcut" in body
    assert "Abort" not in body


def test_the_support_block_distinguishes_a_limitation_from_a_failure() -> None:
    """A completed install must not be reported as a failed one."""
    source = _source()
    block = _procedure_body(source, "procedure PrintSupportBlock")
    assert "if KnownLimitation <> '' then" in block
    # The fatal wording stays reachable for a genuine failure.
    assert "----- Modulo install failed: support reference " in block


# ---------------------------------------------------------------------------
# The registry: what the Service Control Manager actually persists.
# ---------------------------------------------------------------------------


def _windows_module(name: str) -> Any:
    """Import a Windows-only stdlib module without tripping the Linux mypy run.

    ``backend/pyproject.toml`` pins ``platform = "linux"``, so ``ctypes.windll``
    and ``winreg`` do not exist as far as the type checker is concerned. These
    helpers are only reached on Windows, behind a skip guard.
    """
    import importlib

    return importlib.import_module(name)


def _is_elevated() -> bool:
    """True when this process can open the Service Control Manager."""
    shell32: Any = _windows_module("ctypes").windll.shell32
    return bool(shell32.IsUserAnAdmin())


def _service_image_path(name: str) -> str | None:
    """The ``ImagePath`` the SCM persisted for ``name``, or None if absent."""
    winreg: Any = _windows_module("winreg")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{name}") as key:
            value: Any = winreg.QueryValueEx(key, "ImagePath")[0]
    except FileNotFoundError:
        return None
    return str(value)


def _run_sc(args: str) -> subprocess.CompletedProcess[str]:
    # argv is reconstructed from the installer source under test, not user input
    return subprocess.run(  # noqa: S603
        f"sc.exe {args}", check=False, shell=False, capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="sc.exe and the Windows service registry only exist on Windows; the command-line proof runs everywhere",
)
def test_sc_exe_persists_a_quoted_image_path() -> None:
    """Execute the reconstructed command and read the stored value back.

    This is the proof the source-level tests cannot give: sc.exe parses the
    command line itself, so only the registry shows what it actually stored.
    ``sc create`` does not require the executable to exist, which is why the
    default install path can be used unchanged.
    """
    if not _is_elevated():
        pytest.skip("registering a service needs an elevated process (Service Control Manager access)")
    name = f"ModuloInstallerTest{os.getpid()}"
    command = _create_command(_source()).replace("create Modulo ", f"create {name} ", 1)
    try:
        created = _run_sc(command)
        assert created.returncode == 0, f"sc.exe create failed ({created.returncode}): {created.stdout}{created.stderr}"
        assert _service_image_path(name) == _EXPECTED_IMAGE_PATH
    finally:
        _run_sc(f"delete {name}")
    assert _service_image_path(name) is None


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
