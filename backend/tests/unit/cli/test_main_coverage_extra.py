"""Extra coverage for the new ``modulo`` CLI commands (FAR-676): the doctor
``--report`` capture/sink, ``modulo env --raw``, and the ``logs`` rotate /
missing-file paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

import farna.cli.main as cli_main
from farna.launcher.manifest import ManifestSecurityError
from farna.launcher.upgrade import UpgradeError
from farna.support.bundle import BundleTooLargeError, RedactionGateError


def test_doctor_report_builds_archive(tmp_path: Path):
    report = tmp_path / "report.zip"
    result = CliRunner().invoke(cli_main.cli, ["doctor", "--data-dir", str(tmp_path), "--report", str(report)])
    # Uninitialized data dir -> doctor exits 3, but the report archive is built.
    assert result.exit_code == 3
    assert report.is_file()
    import zipfile

    with zipfile.ZipFile(report) as archive:
        names = archive.namelist()
    assert "report.json" in names
    assert "doctor.txt" in names


def test_logs_rotate_child_refused(tmp_path: Path):
    result = CliRunner().invoke(cli_main.cli, ["logs", "--rotate", "postgres", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "rotation applies to the app log only" in result.output


def test_logs_missing_file(tmp_path: Path):
    result = CliRunner().invoke(cli_main.cli, ["logs", "app", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no app log file" in result.output


def test_env_raw():
    result = CliRunner().invoke(cli_main.cli, ["env", "--raw"])
    # --raw succeeds (the warning is printed to stderr); settings must load.
    assert result.exit_code == 0


def test_env_settings_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("farna.settings.get_settings", lambda: (_ for _ in ()).throw(RuntimeError("settings boom")))
    result = CliRunner().invoke(cli_main.cli, ["env"])
    assert result.exit_code == 1
    assert "settings unavailable" in result.output


def test_package_version_exception(monkeypatch: pytest.MonkeyPatch):
    """Test _package_version fallback when importlib.metadata.version fails."""
    monkeypatch.setattr(
        "importlib.metadata.version",
        lambda _: (_ for _ in ()).throw(Exception("version not found")),
    )
    assert cli_main._package_version() == "unknown"


def test_resolve_data_dir_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test _resolve_data_dir raises ClickException when default_data_dir fails."""
    monkeypatch.setattr(
        "farna.launcher.entry.default_data_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("no data dir")),
    )
    CliRunner().invoke(cli_main.cli, ["start", "--data-dir", str(tmp_path)])
    # Should not crash - data_dir is provided explicitly
    # The exception path is when data_dir is None
    with pytest.raises(cli_main.click.ClickException, match="no data dir"):
        cli_main._resolve_data_dir(None)


def test_start_repair_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test start command repair_current_symlink path (when data_dir is None)."""
    mock_run_start = MagicMock(return_value=0)
    monkeypatch.setattr("farna.launcher.entry.run_start", mock_run_start)
    mock_repair = MagicMock(return_value="1.0.0")
    monkeypatch.setattr("farna.launcher.upgrade.repair_current_symlink", mock_repair)
    # Mock default_data_dir to return tmp_path so data_dir=None resolves to it
    monkeypatch.setattr("farna.launcher.entry.default_data_dir", lambda: tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["start"])
    assert result.exit_code == 0
    mock_repair.assert_called_once()


def test_status_branches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test status command various branches (degraded, launcher, components)."""
    mock_collect = MagicMock(
        return_value={
            "data_dir": str(tmp_path),
            "error": "some error",
            "degraded": {"reason": "test degraded"},
            "initialized": True,
            "launcher": {"pid": 123, "mode": "foreground", "alive": True},
            "components": {
                "postgres": {"pid": 456, "port": 5432, "alive": True, "remediation": "check config"},
                "redis": {"pid": 789, "port": 6379, "alive": False},
                "api": {"pid": 999, "port": 8000, "alive": True},
            },
        }
    )
    monkeypatch.setattr("farna.launcher.supervisor.collect_status", mock_collect)

    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "some error" in result.output
    assert "DEGRADED: test degraded" in result.output
    assert "resume with: modulo start --clear-degraded" in result.output
    assert "initialized: True" in result.output
    assert "launcher: pid=123" in result.output
    assert "postgres" in result.output
    assert "redis" in result.output
    assert "hint (postgres): check config" in result.output


def test_status_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test status command with --json flag."""
    mock_collect = MagicMock(
        return_value={
            "data_dir": str(tmp_path),
            "initialized": False,
            "components": {},
        }
    )
    monkeypatch.setattr("farna.launcher.supervisor.collect_status", mock_collect)

    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data_dir"] == str(tmp_path)
    assert data["initialized"] is False


def test_launcher_data_dir_or_none_exception(monkeypatch: pytest.MonkeyPatch):
    """Test _launcher_data_dir_or_none returns None on exception."""
    monkeypatch.setattr(
        "farna.launcher.entry.default_data_dir",
        lambda: (_ for _ in ()).throw(Exception("fail")),
    )
    assert cli_main._launcher_data_dir_or_none(None) is None


def test_launcher_data_dir_or_none_not_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test _launcher_data_dir_or_none returns None when path is not a dir."""
    file_path = tmp_path / "notadir"
    file_path.write_text("x")
    monkeypatch.setattr("farna.launcher.entry.default_data_dir", lambda: file_path)
    assert cli_main._launcher_data_dir_or_none(None) is None


def test_support_redaction_gate_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test support command RedactionGateError path."""
    mock_build = AsyncMock()
    mock_build.side_effect = RedactionGateError(member="test_member", reasons=["reason1", "reason2"])
    monkeypatch.setattr("farna.support.bundle.build_bundle_async", mock_build)
    monkeypatch.setattr("farna.support.bundle.generate_ref", lambda: "test-ref")

    result = CliRunner().invoke(
        cli_main.cli, ["support", "--data-dir", str(tmp_path), "--out", str(tmp_path / "out.zip")]
    )
    assert result.exit_code == 2
    assert "refused: redaction gate refused member" in result.output
    assert "reason1" in result.output
    assert "reason2" in result.output
    assert "no bundle written" in result.output


def test_support_bundle_too_large(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test support command BundleTooLargeError path."""
    mock_build = AsyncMock()
    mock_build.side_effect = BundleTooLargeError("too big")
    monkeypatch.setattr("farna.support.bundle.build_bundle_async", mock_build)
    monkeypatch.setattr("farna.support.bundle.generate_ref", lambda: "test-ref")

    result = CliRunner().invoke(
        cli_main.cli, ["support", "--data-dir", str(tmp_path), "--out", str(tmp_path / "out.zip")]
    )
    assert result.exit_code == 1
    assert "error: could not write bundle: too big" in result.output


def test_support_os_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test support command OSError path."""
    mock_build = AsyncMock()
    mock_build.side_effect = OSError("permission denied")
    monkeypatch.setattr("farna.support.bundle.build_bundle_async", mock_build)
    monkeypatch.setattr("farna.support.bundle.generate_ref", lambda: "test-ref")

    result = CliRunner().invoke(
        cli_main.cli, ["support", "--data-dir", str(tmp_path), "--out", str(tmp_path / "out.zip")]
    )
    assert result.exit_code == 1
    assert "error: could not write bundle: permission denied" in result.output


def test_support_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test support command with --json flag."""
    mock_result = MagicMock()
    mock_result.to_json_dict.return_value = {"ref": "test-ref", "size": 123}
    mock_build = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("farna.support.bundle.build_bundle_async", mock_build)
    monkeypatch.setattr("farna.support.bundle.generate_ref", lambda: "test-ref")

    result = CliRunner().invoke(
        cli_main.cli,
        ["support", "--data-dir", str(tmp_path), "--out", str(tmp_path / "out.zip"), "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ref"] == "test-ref"


def test_logs_follow_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test logs command follow path (simulated)."""
    log_file = tmp_path / "app.log"
    log_file.write_text("test log line\n")

    follow_called = {"called": False}

    def mock_follow(path):
        follow_called["called"] = True

    monkeypatch.setattr("farna.cli.main._follow_log", mock_follow)
    monkeypatch.setattr("farna.launcher.supervisor.log_paths", lambda _: {"app": log_file})

    result = CliRunner().invoke(cli_main.cli, ["logs", "app", "--data-dir", str(tmp_path), "-f"])
    # The follow path will call _follow_log which we mocked
    assert result.exit_code == 0
    assert follow_called["called"]


def test_rotate_log_or_exit_child_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Test _rotate_log_or_exit for non-app component."""
    log_file = tmp_path / "postgres.log"
    log_file.write_text("log")

    cli_main._rotate_log_or_exit(tmp_path, log_file, "postgres")

    assert "rotation applies to the app log only" in capsys.readouterr().out


def test_rotate_log_or_exit_locked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test _rotate_log_or_exit refuses when launcher holds lock."""
    log_file = tmp_path / "app.log"
    log_file.write_text("log")

    mock_read_lock = MagicMock(return_value=MagicMock(pid=123))
    mock_pid_alive = MagicMock(return_value=True)
    monkeypatch.setattr("farna.launcher.supervisor._read_lock_holder", mock_read_lock)
    monkeypatch.setattr("farna.launcher.supervisor._pid_alive", mock_pid_alive)

    result = CliRunner().invoke(cli_main.cli, ["logs", "--rotate", "app", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "refused: the launcher is still running" in result.output


def test_service_install_warnings(monkeypatch: pytest.MonkeyPatch):
    """Test service install callback with warnings."""
    mock_result = MagicMock()
    mock_result.unit_path = "/etc/systemd/user/modulo.service"
    mock_result.executable = "/usr/bin/modulo"
    mock_result.enabled = True
    mock_result.started = True
    mock_result.linger_enabled = True
    mock_result.warnings = ["warning1", "warning2"]
    monkeypatch.setattr("farna.launcher.service.install", lambda: mock_result)

    result = CliRunner().invoke(cli_main.cli, ["service", "install"])
    assert result.exit_code == 0
    assert "installed" in result.output
    assert "unit enabled: true" in result.output
    assert "started: true" in result.output
    assert "linger: true" in result.output
    assert "warning: warning1" in result.output
    assert "warning: warning2" in result.output


def test_service_uninstall_warnings(monkeypatch: pytest.MonkeyPatch):
    """Test service uninstall callback with warnings."""
    mock_result = MagicMock()
    mock_result.unit_removed = True
    mock_result.stopped = True
    mock_result.disabled = True
    mock_result.linger_disabled = True
    mock_result.warnings = ["uninstall warning"]
    monkeypatch.setattr("farna.launcher.service.uninstall", lambda: mock_result)

    result = CliRunner().invoke(cli_main.cli, ["service", "uninstall"])
    assert result.exit_code == 0
    assert "uninstalled" in result.output
    assert "removed: true" in result.output
    assert "stopped: true" in result.output
    assert "disabled: true" in result.output
    assert "linger disabled: true" in result.output
    assert "warning: uninstall warning" in result.output


def test_service_status_error(monkeypatch: pytest.MonkeyPatch):
    """Test service status command error path."""
    monkeypatch.setattr(
        "farna.launcher.service.status",
        lambda **_: (_ for _ in ()).throw(RuntimeError("service status failed")),
    )

    result = CliRunner().invoke(cli_main.cli, ["service", "status"])
    assert result.exit_code == 1
    assert "service status failed" in result.output


def test_service_status_launcher_error(monkeypatch: pytest.MonkeyPatch):
    """Test service status with launcher error in payload."""
    mock_status = MagicMock(
        return_value={
            "unit": "modulo.service",
            "unit_path": "/etc/systemd/user/modulo.service",
            "enabled": True,
            "active": "active",
            "linger": "enabled",
            "launcher": {"error": "launcher not found"},
        }
    )
    monkeypatch.setattr("farna.launcher.service.status", mock_status)

    result = CliRunner().invoke(cli_main.cli, ["service", "status"])
    assert result.exit_code == 0
    assert "launcher: launcher not found" in result.output


def test_service_status_launcher_ok(monkeypatch: pytest.MonkeyPatch):
    """Test service status with launcher info."""
    mock_status = MagicMock(
        return_value={
            "unit": "modulo.service",
            "unit_path": "/etc/systemd/user/modulo.service",
            "enabled": True,
            "active": "active",
            "linger": "enabled",
            "launcher": {
                "launcher": {"alive": True, "pid": 456},
                "initialized": True,
            },
        }
    )
    monkeypatch.setattr("farna.launcher.service.status", mock_status)

    result = CliRunner().invoke(cli_main.cli, ["service", "status"])
    assert result.exit_code == 0
    assert "launcher: alive=true pid=456 initialized=true" in result.output


def test_clear_degraded_os_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test clear-degraded command OSError path."""
    monkeypatch.setattr(
        "farna.launcher.supervisor.clear_degraded_record",
        lambda _: (_ for _ in ()).throw(OSError("permission denied")),
    )

    result = CliRunner().invoke(cli_main.cli, ["clear-degraded", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "could not clear the degraded state" in result.output


def test_clear_degraded_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test clear-degraded command when state is removed."""
    monkeypatch.setattr("farna.launcher.supervisor.clear_degraded_record", lambda _: True)

    result = CliRunner().invoke(cli_main.cli, ["clear-degraded", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "terminal-degraded state cleared" in result.output


def test_clear_degraded_not_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test clear-degraded command when no state present."""
    monkeypatch.setattr("farna.launcher.supervisor.clear_degraded_record", lambda _: False)

    result = CliRunner().invoke(cli_main.cli, ["clear-degraded", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "no terminal-degraded state present" in result.output


def test_upgrade_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test upgrade command success path."""
    mock_result = MagicMock()
    mock_result.previous_version = "1.0.0"
    mock_result.version = "2.0.0"
    mock_result.snapshot = tmp_path / "snapshot.sql"
    monkeypatch.setattr("farna.launcher.upgrade.perform_upgrade", lambda *a, **k: mock_result)
    monkeypatch.setattr("farna.launcher.upgrade.default_install_root", lambda: tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["upgrade", "2.0.0", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "upgraded 1.0.0 -> 2.0.0" in result.output
    assert "pre-upgrade snapshot" in result.output


def test_upgrade_no_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test upgrade command when no snapshot returned."""
    mock_result = MagicMock()
    mock_result.previous_version = "1.0.0"
    mock_result.version = "2.0.0"
    mock_result.snapshot = None
    monkeypatch.setattr("farna.launcher.upgrade.perform_upgrade", lambda *a, **k: mock_result)
    monkeypatch.setattr("farna.launcher.upgrade.default_install_root", lambda: tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["upgrade", "2.0.0", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "upgraded 1.0.0 -> 2.0.0" in result.output
    assert "pre-upgrade snapshot" not in result.output


def test_upgrade_upgrade_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test upgrade command UpgradeError path."""
    monkeypatch.setattr(
        "farna.launcher.upgrade.perform_upgrade",
        lambda *a, **k: (_ for _ in ()).throw(UpgradeError("upgrade failed")),
    )
    monkeypatch.setattr("farna.launcher.upgrade.default_install_root", lambda: tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["upgrade", "2.0.0", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "upgrade failed" in result.output


def test_upgrade_manifest_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test upgrade command ManifestSecurityError path."""
    monkeypatch.setattr(
        "farna.launcher.upgrade.perform_upgrade",
        lambda *a, **k: (_ for _ in ()).throw(ManifestSecurityError("manifest invalid")),
    )
    monkeypatch.setattr("farna.launcher.upgrade.default_install_root", lambda: tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["upgrade", "2.0.0", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "manifest invalid" in result.output


def test_init_once_register_legacy_commands_duplicate(monkeypatch: pytest.MonkeyPatch):
    """Test _init_once_register_legacy_commands raises on duplicate."""
    # Create a command that's already registered
    import click

    dummy_cmd = click.Command("backup")
    monkeypatch.setattr(cli_main.cli, "commands", {"backup": dummy_cmd})
    monkeypatch.setattr(
        "farna.cli.backup.cli",
        MagicMock(commands={"backup": click.Command("backup")}),
    )

    with pytest.raises(RuntimeError, match="already registered on the modulo group"):
        cli_main._init_once_register_legacy_commands()


def test_init_once_register_users_command_duplicate(monkeypatch: pytest.MonkeyPatch):
    """Test _init_once_register_users_command raises on duplicate."""
    import click

    dummy_cmd = click.Command("users")
    monkeypatch.setattr(cli_main.cli, "commands", {"users": dummy_cmd})
    monkeypatch.setattr("farna.cli.users.users", click.Command("users"))

    with pytest.raises(RuntimeError, match="already registered on the modulo group"):
        cli_main._init_once_register_users_command()


def test_init_once_register_evidence_command_duplicate(monkeypatch: pytest.MonkeyPatch):
    """Test _init_once_register_evidence_command raises on duplicate."""
    import click

    dummy_cmd = click.Command("evidence")
    monkeypatch.setattr(cli_main.cli, "commands", {"evidence": dummy_cmd})
    monkeypatch.setattr("farna.cli.evidence.evidence", click.Command("evidence"))

    with pytest.raises(RuntimeError, match="already registered on the modulo group"):
        cli_main._init_once_register_evidence_command()


def test_init_once_register_backfill_base_url_command_duplicate(monkeypatch: pytest.MonkeyPatch):
    """Test _init_once_register_backfill_base_url_command raises on duplicate."""
    import click

    dummy_cmd = click.Command("backfill-model-backend-base-url")
    monkeypatch.setattr(cli_main.cli, "commands", {"backfill-model-backend-base-url": dummy_cmd})
    monkeypatch.setattr(
        "farna.cli.backfill_model_backend_base_url.backfill_model_backend_base_url",
        click.Command("backfill-model-backend-base-url"),
    )

    with pytest.raises(RuntimeError, match="already registered on the modulo group"):
        cli_main._init_once_register_backfill_base_url_command()


def test_status_components_not_dict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test status command when components is not a dict."""
    mock_collect = MagicMock(
        return_value={
            "data_dir": str(tmp_path),
            "initialized": True,
            "components": "not a dict",
        }
    )
    monkeypatch.setattr("farna.launcher.supervisor.collect_status", mock_collect)

    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    # Should not crash, components should be treated as empty


def test_status_component_not_dict_continue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test status command when a component is not a dict (continue branch)."""
    mock_collect = MagicMock(
        return_value={
            "data_dir": str(tmp_path),
            "initialized": True,
            "components": {
                "postgres": {"pid": 456, "port": 5432, "alive": True},
                "redis": "not a dict",  # This should trigger the continue branch
            },
        }
    )
    monkeypatch.setattr("farna.launcher.supervisor.collect_status", mock_collect)

    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "postgres" in result.output
    # redis should be skipped due to continue


def test_support_summary_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test support command summary_lines loop."""
    mock_result = MagicMock()
    mock_result.to_json_dict.return_value = {"ref": "test-ref"}
    mock_build = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("farna.support.bundle.build_bundle_async", mock_build)
    monkeypatch.setattr("farna.support.bundle.generate_ref", lambda: "test-ref")
    monkeypatch.setattr("farna.support.bundle.summary_lines", lambda r: ["line1", "line2"])

    result = CliRunner().invoke(
        cli_main.cli,
        ["support", "--data-dir", str(tmp_path), "--out", str(tmp_path / "out.zip")],
    )
    assert result.exit_code == 0
    assert "line1" in result.output
    assert "line2" in result.output


def test_logs_follow_path_direct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test _echo_log_contents with follow=True passes the log path."""
    log_file = tmp_path / "app.log"
    log_file.write_text("test log line\n")
    observed_paths: list[Path] = []

    def mock_follow(path):
        observed_paths.append(path)

    monkeypatch.setattr("farna.cli.main._follow_log", mock_follow)
    cli_main._echo_log_contents(log_file, follow=True)

    assert observed_paths == [log_file]


def test_follow_log_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test _follow_log handles KeyboardInterrupt."""
    log_file = tmp_path / "app.log"
    log_file.write_text("test\n")

    # Mock time.sleep to raise KeyboardInterrupt immediately
    def mock_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", mock_sleep)

    # This should not raise
    cli_main._follow_log(log_file)


def test_follow_log_os_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test _follow_log handles OSError."""
    log_file = tmp_path / "app.log"
    log_file.write_text("test\n")

    call_count = {"count": 0}

    def mock_sleep(_):
        call_count["count"] += 1
        if call_count["count"] > 1:
            raise KeyboardInterrupt  # Exit the loop after OSError

    def mock_stat(self, follow_symlinks=True):
        class MockStat:
            st_size = 100

        if call_count["count"] == 0:
            return MockStat()
        # Second call - simulate file growth then error
        raise OSError("read failed")

    monkeypatch.setattr("time.sleep", mock_sleep)
    monkeypatch.setattr(Path, "stat", mock_stat)
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"data")

    # Should exit with SystemExit(1)
    with pytest.raises(SystemExit) as exc_info:
        cli_main._follow_log(log_file)
    assert exc_info.value.code == 1


def test_service_status_launcher_error_branch(monkeypatch: pytest.MonkeyPatch):
    """Test service status with launcher error and no unit metadata."""
    mock_status = MagicMock(
        return_value={
            "unit": None,
            "unit_path": None,
            "enabled": False,
            "active": False,
            "linger": False,
            "launcher": {"error": "launcher not found"},
        }
    )
    monkeypatch.setattr("farna.launcher.service.status", mock_status)

    result = CliRunner().invoke(cli_main.cli, ["service", "status"])

    assert result.exit_code == 0
    assert "unit: None (None)" in result.output
    assert "launcher: launcher not found" in result.output
