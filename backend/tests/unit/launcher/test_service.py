"""Unit tests for the systemd service install surface (FAR-674).

Locks: the unit content (exec path, restart budget co-designed with the
launcher's crash cap, SuccessExitStatus degraded contract), the
non-Linux/non-systemd refusals, the elevated-install refusal, the
install/uninstall/status round-trips over the runner seam, the pkexec
linger attempt with the documented sudo fallback, and a POSIX-only
fake-bin round-trip with mocked ``systemctl``/``loginctl``/``pkexec`` on
PATH (real systemd integration is CI's job).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from modulo.launcher import policy as policy_module
from modulo.launcher.service import (
    ServiceError,
    assert_linux,
    assert_not_elevated,
    assert_systemd_available,
    install,
    render_unit,
    resolve_executable,
    resolve_username,
    status,
    uninstall,
)


class FakeRunner:
    """Records argvs and returns canned results per prefix."""

    def __init__(self, results: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = results if results is not None else {}

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        for prefix, (code, out) in self._results.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runner-seam tests exercise Linux logic on any OS."""
    if sys.platform != "linux":
        monkeypatch.setattr(sys, "platform", "linux")


def _executable(tmp_path: Path) -> Path:
    executable = tmp_path / "bin" / "modulo"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


# ---------------------------------------------------------------------------
# Unit content (service-mode contract)
# ---------------------------------------------------------------------------


def test_unit_runs_the_installed_binary_in_foreground(tmp_path: Path) -> None:
    executable = tmp_path / "venv" / "bin" / "modulo"
    unit = render_unit(executable)
    assert f"ExecStart={executable} start" in unit
    assert "Type=simple" in unit
    assert "WantedBy=default.target" in unit


def test_unit_owns_launchers_restart_not_children(tmp_path: Path) -> None:
    unit = render_unit(tmp_path / "modulo")
    assert "Restart=on-failure" in unit
    # Documented in the unit itself: the in-app supervisor owns child restart.
    assert "in-app supervisor owns CHILD restart" in unit


def test_unit_exit_code_contract_marks_terminal_degraded(tmp_path: Path) -> None:
    """Degraded exit is listed in SuccessExitStatus so systemd stays STOPPED."""
    unit = render_unit(tmp_path / "modulo")
    assert f"SuccessExitStatus={policy_module.DEGRADED_EXIT_CODE}" in unit
    # The launcher's degraded exit is conceptually a stop, not a failure for
    # the OS manager: it is neither 0 nor the generic boot-failure 1.
    assert policy_module.DEGRADED_EXIT_CODE not in (0, 1)


def test_unit_start_limit_budget_mirrors_the_launcher_crash_cap(tmp_path: Path) -> None:
    unit = render_unit(tmp_path / "modulo")
    assert f"StartLimitIntervalSec={int(policy_module.CRASH_WINDOW_SECONDS)}s" in unit
    assert f"StartLimitBurst={policy_module.CRASH_CAP}" in unit


def test_unit_restart_sec_comes_from_the_shared_policy(tmp_path: Path) -> None:
    """RestartSec comes from the shared policy module, not an inline number."""
    unit = render_unit(tmp_path / "modulo")
    assert f"RestartSec={int(policy_module.SERVICE_RESTART_SECONDS)}s" in unit


# ---------------------------------------------------------------------------
# Platform + manager guards
# ---------------------------------------------------------------------------


def test_macos_is_refused_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ServiceError, match="FAR-678"):
        assert_linux()


def test_windows_is_refused_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ServiceError, match=r"TODO\(P3\)"):
        assert_linux()


def test_non_systemd_host_refuses_and_names_the_manager(tmp_path: Path) -> None:
    missing_marker = tmp_path / "no-systemd-here"
    runner = FakeRunner({("systemctl", "--user", "is-system-running"): (1, "off")})
    with pytest.raises(ServiceError, match="systemd was not detected"):
        assert_systemd_available(runner, marker_path=missing_marker)


def test_systemd_marker_present_short_circuits(tmp_path: Path) -> None:
    marker = tmp_path / "systemd-system"
    marker.mkdir()
    runner = FakeRunner()
    assert_systemd_available(runner, marker_path=marker)
    assert not runner.calls  # marker decided; no probe needed


def test_elevated_install_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import os as os_module

    monkeypatch.setattr(os_module, "getuid", lambda: 0, raising=False)
    with pytest.raises(ServiceError, match="root/sudo"):
        assert_not_elevated()


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------


def test_resolve_executable_uses_argv0(tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "modulo"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    assert resolve_executable(str(fake)) == fake


def test_resolve_executable_falls_back_to_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    name = "modulo.exe" if sys.platform == "win32" else "modulo"
    fake = fake_bin / name
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]))
    assert resolve_executable(str(tmp_path / "missing")) == fake.resolve()


def test_resolve_executable_without_any_candidate_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises(ServiceError, match="could not resolve"):
        resolve_executable(str(tmp_path / "missing"))


# ---------------------------------------------------------------------------
# Install / uninstall / status round-trips (runner seam)
# ---------------------------------------------------------------------------


def test_install_round_trip_writes_enables_and_enables_linger(linux_only: None, tmp_path: Path) -> None:
    executable = _executable(tmp_path)
    unit_path = tmp_path / "systemd" / "user" / "modulo.service"
    runner = FakeRunner()
    linger_seen: list[list[str]] = []

    def linger_argv(username: str) -> list[str]:
        linger_seen.append(["pkexec", "loginctl", "enable-linger", username])
        return linger_seen[-1]

    result = install(
        runner=runner,
        unit_path=unit_path,
        executable=executable,
        linger_argv_builder=linger_argv,
    )
    assert result.unit_path == unit_path
    assert result.enabled is True
    assert result.started is True
    assert result.linger_enabled is True
    assert not result.warnings
    content = unit_path.read_text(encoding="utf-8")
    assert f"ExecStart={executable} start" in content
    reloaded = [argv for argv in runner.calls if argv[:3] == ["systemctl", "--user", "daemon-reload"]]
    assert reloaded
    assert ["systemctl", "--user", "enable", "--now", "modulo.service"] in [list(argv) for argv in runner.calls]
    assert linger_seen[0][3] == resolve_username()


def test_install_linger_pkexec_failure_prints_sudo_fallback(linux_only: None, tmp_path: Path) -> None:
    executable = _executable(tmp_path)
    runner = FakeRunner({("pkexec", "loginctl"): (1, "not authorized")})
    result = install(
        runner=runner,
        unit_path=tmp_path / "u" / "modulo.service",
        executable=executable,
    )
    assert result.linger_enabled is False
    assert result.enabled is True  # the install still succeeded
    assert any("sudo loginctl enable-linger" in warning for warning in result.warnings)


def test_install_daemon_reload_failure_is_warned(linux_only: None, tmp_path: Path) -> None:
    executable = _executable(tmp_path)
    runner = FakeRunner({("systemctl", "--user", "daemon-reload"): (5, "")})
    result = install(
        runner=runner,
        unit_path=tmp_path / "u" / "modulo.service",
        executable=executable,
    )
    assert any("daemon-reload" in warning for warning in result.warnings)


def test_uninstall_round_trip_stops_disables_and_disables_linger(linux_only: None, tmp_path: Path) -> None:
    unit_path = tmp_path / "systemd" / "user" / "modulo.service"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text(render_unit(tmp_path / "modulo"), encoding="utf-8")
    runner = FakeRunner()
    result = uninstall(
        runner=runner,
        unit_path=unit_path,
        linger_argv_builder=lambda user: ["pkexec", "loginctl", "disable-linger", user],
    )
    assert result.stopped is True
    assert result.disabled is True
    assert result.unit_removed is True
    assert result.linger_disabled is True
    assert not unit_path.exists()
    assert runner.calls[0] == ["systemctl", "--user", "stop", "modulo.service"]
    assert runner.calls[1] == ["systemctl", "--user", "disable", "modulo.service"]


def test_uninstall_tolerates_a_missing_unit(linux_only: None, tmp_path: Path) -> None:
    runner = FakeRunner({("systemctl", "--user", "stop"): (1, "not loaded")})
    result = uninstall(
        runner=runner,
        unit_path=tmp_path / "gone" / "modulo.service",
        linger_argv_builder=lambda user: ["pkexec", "loginctl", "disable-linger", user],
    )
    assert result.unit_removed is True  # unlink(missing_ok=True)
    assert result.stopped is False


def test_status_surfaces_enabled_active_linger_and_launcher_health(linux_only: None, tmp_path: Path) -> None:
    import modulo.launcher.supervisor as supervisor_module

    collected: list[Path] = []

    def fake_collect(data_dir: Path) -> dict[str, object]:
        collected.append(data_dir)
        return {"data_dir": str(data_dir), "initialized": False}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(supervisor_module, "collect_status", fake_collect)
        runner = FakeRunner(
            {
                ("systemctl", "--user", "is-enabled"): (0, "enabled\n"),
                ("loginctl", "show-user"): (0, "Linger=yes\n"),
            }
        )
        payload = status(runner=runner, data_dir=tmp_path / "data")
    finally:
        monkeypatch.undo()
    assert payload["enabled"] == "enabled"
    assert payload["linger"] == "enabled"
    assert payload["unit"] == "modulo.service"
    assert collected == [tmp_path / "data"]


# ---------------------------------------------------------------------------
# Fake-bin round-trip on PATH (POSIX CI; real systemd is CI's job)
# ---------------------------------------------------------------------------

requires_posix = pytest.mark.skipif(sys.platform == "win32", reason="fake-shell executables need POSIX")


@requires_posix
def test_install_against_a_fake_systemd_bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The PATH-runner invokes the real-looking fake `systemctl`/`pkexec`."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    log = fake_bin / "calls.log"
    log_literal = str(log)

    def get_executable_script() -> str:
        return f"#!/bin/sh\necho \"$@\" >> '{log_literal}'\n"

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(get_executable_script(), encoding="utf-8")
    systemctl.chmod(0o755)
    pkexec = fake_bin / "pkexec"
    pkexec.write_text(get_executable_script(), encoding="utf-8")
    pkexec.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]))

    executable = _executable(tmp_path)
    unit_path = tmp_path / "systemd" / "user" / "modulo.service"
    import modulo.launcher.service as service_module

    result = service_module.install(unit_path=unit_path, executable=executable)
    assert result.enabled is True
    assert result.linger_enabled is True
    calls = log.read_text(encoding="utf-8").splitlines()
    assert "--user enable --now modulo.service" in calls
    assert any(call == "loginctl enable-linger " + resolve_username() for call in calls)
    assert unit_path.exists()
