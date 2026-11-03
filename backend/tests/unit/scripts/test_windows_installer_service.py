"""FAR-2346 - Alpha installs a foreground shortcut and never registers a service.

Windows service mode is deferred to Beta (FAR-2347). The launcher has no Windows
service seam yet (``TODO(P3)`` in ``backend/src/modulo/launcher/entry.py``) and
``backend/src/modulo/launcher/service.py`` refuses on Windows, so an installer
that runs ``sc.exe create`` on every install attempts a known-impossible action.
Alpha's launch path is the Start Menu shortcut, which runs ``modulo.exe start``
in the foreground.

This suite pins the resulting contract in ``deploy/windows/modulo.iss``:

* nothing invokes ``sc.exe``, and there is no uninstall step that stops or
  deletes a service, because no service is ever created;
* the Alpha launch path is unchanged - the bundle's ``bin\\modulo.exe`` is
  declared, copied to ``{app}\\bin`` and linked from the Start Menu and the
  optional desktop shortcut;
* the deferred feature is stated where the user actually reads it - the
  pre-install info file and the installer's own log - and is not offered as a
  task the user could tick;
* there is no ``Abort;`` in ``[Code]``: the post-install stage cannot fail.

The ``[Setup]`` architecture-identifier tests are kept because that section
still exists and the identifier must stay non-deprecated.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ISS = _REPO_ROOT / "deploy" / "windows" / "modulo.iss"
_INFO = _REPO_ROOT / "deploy" / "windows" / "INSTALLER-INFO.txt"

# The exact wording the user must see in the info file and in the install log.
# "Coming soon" and the "(Beta)" milestone make it non-interactive: the feature
# does not exist, so there is nothing to select.
_SERVICE_MODE_STATEMENT = "Windows service mode: Coming soon (Beta)"

# The launcher resolves the bundle root as the parent of its own directory, so
# every shortcut and the [Files] entry must point at {app}\bin\modulo.exe.
_LAUNCHER_DEST = r"{app}\bin"
_LAUNCHER_SOURCE = r"{#BUNDLE_DIR}\bin\modulo.exe"


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


# ---------------------------------------------------------------------------
# No service is registered, started, stopped or deleted.
# ---------------------------------------------------------------------------


def test_the_installer_never_invokes_sc_exe() -> None:
    """A known-impossible ``sc.exe create`` must not be attempted at all."""
    assert "sc.exe" not in _source().lower()


def test_the_installer_has_no_uninstall_run_section() -> None:
    """No service exists, so there is no service step to run on uninstall.

    The section header is matched at line start, so the explanatory comment in
    the source (which names the section) is not a false positive.
    """
    source = _source()
    assert re.search(r"^\[UninstallRun\]", source, re.MULTILINE) is None


# ---------------------------------------------------------------------------
# The Alpha launch path: the bundle launcher and the two shortcuts.
# ---------------------------------------------------------------------------


def test_the_installer_declares_and_copies_the_bundle_launcher() -> None:
    files = _section_entries(_source(), "Files")
    launcher_entries = [entry for entry in files if _LAUNCHER_SOURCE in entry]
    assert len(launcher_entries) == 1
    assert f'DestDir: "{_LAUNCHER_DEST}"' in launcher_entries[0]


def test_the_installer_creates_a_start_menu_shortcut_for_the_launcher() -> None:
    icons = _section_entries(_source(), "Icons")
    start_menu = [entry for entry in icons if 'Name: "{group}\\Modulo"' in entry]
    assert len(start_menu) == 1
    assert f'Filename: "{_LAUNCHER_DEST}\\modulo.exe"' in start_menu[0]
    assert 'WorkingDir: "{app}"' in start_menu[0]


def test_the_installer_creates_an_optional_desktop_shortcut_for_the_launcher() -> None:
    icons = _section_entries(_source(), "Icons")
    desktop = [entry for entry in icons if 'Name: "{commondesktop}\\Modulo"' in entry]
    assert len(desktop) == 1
    assert f'Filename: "{_LAUNCHER_DEST}\\modulo.exe"' in desktop[0]
    assert 'WorkingDir: "{app}"' in desktop[0]
    assert "Tasks: desktopicon" in desktop[0]


# ---------------------------------------------------------------------------
# The limitation is stated, non-interactively, where the user reads it.
# ---------------------------------------------------------------------------


def test_the_info_file_states_the_coming_soon_limitation() -> None:
    assert _INFO.is_file(), f"the Windows installer info file is missing: {_INFO}"
    info = _INFO.read_text(encoding="utf-8")
    assert _SERVICE_MODE_STATEMENT in info
    assert "does not register a Windows service" in info


def test_the_info_file_no_longer_claims_a_service_is_registered() -> None:
    info = _INFO.read_text(encoding="utf-8")
    assert "registers it as a Windows service" not in info


def test_the_install_log_records_the_coming_soon_limitation() -> None:
    source = _source()
    assert "procedure WriteServiceModeNotice" in source
    notice = _procedure_body(source, "procedure WriteServiceModeNotice")
    assert "WriteInstallLog(" in notice
    assert _SERVICE_MODE_STATEMENT in notice


def test_the_post_install_stage_writes_the_limitation() -> None:
    body = _procedure_body(_source(), "procedure CurStepChanged")
    start = body.index("ssPostInstall")
    end = body.index("ssDone")
    assert "WriteServiceModeNotice;" in body[start:end]


def test_the_limitation_is_not_a_task_the_user_can_tick() -> None:
    """The feature does not exist, so it must not be a selectable option."""
    tasks = _section_entries(_source(), "Tasks")
    assert len(tasks) == 1
    assert 'Name: "desktopicon"' in tasks[0]
    assert "service" not in tasks[0].lower()


def test_the_post_install_stage_cannot_abort() -> None:
    source = _source()
    code = source[source.index("[Code]") :]
    assert "Abort" not in code


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
