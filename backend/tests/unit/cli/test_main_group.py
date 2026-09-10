"""Unit tests for the top-level ``modulo`` CLI group (FAR-671 slice 2).

Locks the published-surface compatibility: bare ``modulo backup`` /
``modulo restore`` / ``modulo apply`` keep working through the new group
(the backup commands are lifted onto it and an unrecognised first token
falls back to ``backup``), plus the start/stop/status/version/env surface
with mocked launcher internals and the credential redaction discipline.
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

import modulo.cli.main as cli_main
import modulo.settings as settings_module
from modulo.settings import get_settings

FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
SECRET_KEY = "a" * 32


@pytest.fixture(autouse=True)
def _reset_launcher_state() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    settings_module._pinned_env_file = None
    settings_module._first_boot_guard = None
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Published-surface compatibility
# ---------------------------------------------------------------------------


def test_backup_is_registered_and_help_survives() -> None:
    result = CliRunner().invoke(cli_main.cli, ["backup", "--help"])
    assert result.exit_code == 0
    assert "Create a full backup" in result.output


def test_restore_is_registered_and_help_survives() -> None:
    result = CliRunner().invoke(cli_main.cli, ["restore", "--help"])
    assert result.exit_code == 0
    assert "Restore a Modulo database" in result.output


def test_apply_is_registered_and_help_survives() -> None:
    result = CliRunner().invoke(cli_main.cli, ["apply", "--help"])
    assert result.exit_code == 0
    assert "apply" in result.output.lower()


def test_unknown_first_token_falls_back_to_backup() -> None:
    """``modulo --output-dir x --help`` must reach the backup command."""
    result = CliRunner().invoke(cli_main.cli, ["--output-dir", "ignored", "--help"])
    assert result.exit_code == 0
    assert "Create a full backup" in result.output


def test_legacy_backup_group_still_works_standalone() -> None:
    from modulo.cli.backup import cli as legacy_cli

    result = CliRunner().invoke(legacy_cli, ["backup", "--help"])
    assert result.exit_code == 0
    assert "Create a full backup" in result.output


def test_group_help_lists_both_surfaces() -> None:
    result = CliRunner().invoke(cli_main.cli, ["--help"])
    assert result.exit_code == 0
    for command in ("backup", "restore", "apply", "start", "stop", "status", "version", "env"):
        assert command in result.output


def test_launcher_owned_commands_scrub_hostile_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launcher-owned commands re-scrub at invocation (defence-in-depth)."""
    import os

    monkeypatch.setenv("PGHOST", "foreign.example")
    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(Path("unused"))])
    assert result.exit_code == 0
    assert "PGHOST" not in os.environ


def test_published_backup_surface_keeps_inherited_libpq_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scrub is NOT in the group callback: pg_dump/psql need libpq vars."""
    import os

    monkeypatch.setenv("PGHOST", "db.internal.example")
    monkeypatch.setenv("PGPASSWORD", "libpq-secret")
    result = CliRunner().invoke(cli_main.cli, ["version"])
    assert result.exit_code == 0
    assert os.environ.get("PGHOST") == "db.internal.example"
    assert os.environ.get("PGPASSWORD") == "libpq-secret"


def test_console_script_scrubs_before_any_project_import() -> None:
    """The published entry point scrubs BEFORE importing psycopg/settings.

    The console script is ``modulo.cli.main:cli``: its module-level import
    graph (``modulo.cli.backup`` -> settings, apply, psycopg) loads before
    any command runs, so the scrub must be the module's first action —
    verified here on the REAL import path in a fresh subprocess.
    """
    script = "\n".join(
        [
            "import os, sys",
            "os.environ['PGHOST'] = 'foreign.example'",
            "import modulo.cli.main",
            "print(os.environ.get('PGHOST'),",
            "      'modulo.cli.backup' in sys.modules,",
            "      'modulo.launcher.env_safety' in sys.modules)",
        ]
    )
    result = subprocess.run(  # noqa: S603 — test driver
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    pg_host, backup_loaded, env_safety_loaded = result.stdout.split()
    assert pg_host == "None"  # the scrub ran during the module import
    assert backup_loaded == "True"  # the heavy graph loaded AFTER the scrub
    assert env_safety_loaded == "True"


# ---------------------------------------------------------------------------
# version / env
# ---------------------------------------------------------------------------


def test_version_flag_and_subcommand() -> None:
    flag = CliRunner().invoke(cli_main.cli, ["--version"])
    subcommand = CliRunner().invoke(cli_main.cli, ["version"])
    assert flag.exit_code == 0
    assert subcommand.exit_code == 0
    assert flag.output.startswith("modulo ")
    assert subcommand.output.startswith("modulo ")


def test_env_redacts_every_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FERNET_KEY", FERNET_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app-user:super-secret@db-host:5432/modulo")
    result = CliRunner().invoke(cli_main.cli, ["env"])
    assert result.exit_code == 0
    assert "super-secret" not in result.output
    assert SECRET_KEY not in result.output
    assert FERNET_KEY not in result.output
    assert "<redacted>@db-host:5432/modulo" in result.output
    assert "postgresql://<redacted>@db-host:5432/modulo" in result.output
    assert "fernet_key=<redacted>" in result.output
    assert "secret_key=<redacted>" in result.output


def test_env_json_redacts_every_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FERNET_KEY", FERNET_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app-user:super-secret@db-host:5432/modulo")
    result = CliRunner().invoke(cli_main.cli, ["env", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["database_url"] == "postgresql://<redacted>@db-host:5432/modulo"
    assert payload["fernet_key"] == "<redacted>"


_USER_PASSWORD = "plaintext-password-1"
_OIDC_CLIENT_SECRET = "oidc-client-secret-value"
_WEBHOOK_TOKEN = "hooks/SlackTokenAbCdEf123456"
_AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_LICENSE_KEY = "lic-live-0123456789abcdef"


def _set_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FERNET_KEY", FERNET_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app-user:super-secret@db-host:5432/modulo")
    monkeypatch.setenv("REDIS_URL", "redis://:redis-secret@cache-host:16379/0")
    monkeypatch.setenv("MODULO_USERS", f"admin@modulo.run:{_USER_PASSWORD}")
    monkeypatch.setenv(
        "MODULO_OIDC_PROVIDERS",
        f'[{{"provider_id": "okta", "client_id": "abc", "client_secret": "{_OIDC_CLIENT_SECRET}"}}]',
    )
    monkeypatch.setenv("ALERT_WEBHOOK_URL", f"https://hooks.example.com/{_WEBHOOK_TOKEN}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", _AWS_KEY_ID)
    monkeypatch.setenv("MODULO_LICENSE_KEY", _LICENSE_KEY)


@pytest.mark.parametrize("as_json", [False, True], ids=["table", "json"])
def test_env_redacts_every_real_credential_carrier(monkeypatch: pytest.MonkeyPatch, as_json: bool) -> None:
    """No raw password/secret may survive the env output (table or JSON)."""
    _set_credential_env(monkeypatch)
    argv = ["env", "--json"] if as_json else ["env"]
    result = CliRunner().invoke(cli_main.cli, argv)
    assert result.exit_code == 0
    secrets = (
        _USER_PASSWORD,
        _OIDC_CLIENT_SECRET,
        _WEBHOOK_TOKEN,
        _AWS_KEY_ID,
        _LICENSE_KEY,
        "super-secret",
        "redis-secret",
    )
    for secret in secrets:
        assert secret not in result.output, f"{secret!r} leaked through modulo env"
    assert "admin@modulo.run:plaintext-password-1" not in result.output
    if as_json:
        payload = json.loads(result.output)
        assert payload["modulo_users"] == "<redacted>"
        assert payload["modulo_oidc_providers"] == "<redacted>"
        assert payload["alert_webhook_url"] == "<redacted>"
        assert payload["aws_access_key_id"] == "<redacted>"
        assert payload["modulo_license_key"] == "<redacted>"
        assert payload["redis_url"] == "redis://<redacted>@cache-host:16379/0"


# ---------------------------------------------------------------------------
# start / stop / status (mocked launcher internals)
# ---------------------------------------------------------------------------


def test_start_invokes_run_start_with_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, bool, Path | None]] = []
    import modulo.launcher.entry as entry_module

    def fake_run_start(data_dir: Path | None, *, detach: bool = False, bin_dir: Path | None = None) -> int:
        calls.append((data_dir or Path("unset"), detach, bin_dir))
        return 0

    monkeypatch.setattr(entry_module, "run_start", fake_run_start)
    result = CliRunner().invoke(cli_main.cli, ["start", "--data-dir", str(tmp_path), "--detach"])
    assert result.exit_code == 0
    assert calls == [(tmp_path, True, None)]


def test_start_failure_is_rendered_as_click_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.entry as entry_module

    def failing_run_start(data_dir: Path | None, *, detach: bool = False, bin_dir: Path | None = None) -> int:
        raise RuntimeError("boom: cannot boot")

    monkeypatch.setattr(entry_module, "run_start", failing_run_start)
    result = CliRunner().invoke(cli_main.cli, ["start", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "boom: cannot boot" in result.output


def test_start_keyboard_interrupt_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.entry as entry_module

    def interrupted_run_start(data_dir: Path | None, *, detach: bool = False, bin_dir: Path | None = None) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(entry_module, "run_start", interrupted_run_start)
    result = CliRunner().invoke(cli_main.cli, ["start", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0


def test_stop_invokes_request_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.supervisor as supervisor_module

    calls: list[Path] = []

    def fake_request_stop(data_dir: Path, *, timeout: float = 10.0) -> int:
        calls.append(data_dir)
        return 0

    monkeypatch.setattr(supervisor_module, "request_stop", fake_request_stop)
    result = CliRunner().invoke(cli_main.cli, ["stop", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert calls == [tmp_path]


def test_stop_failure_is_rendered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.supervisor as supervisor_module

    def failing_stop(data_dir: Path, *, timeout: float = 10.0) -> int:
        raise RuntimeError("launcher did not stop")

    monkeypatch.setattr(supervisor_module, "request_stop", failing_stop)
    result = CliRunner().invoke(cli_main.cli, ["stop", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "launcher did not stop" in result.output


def test_status_json_renders_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.supervisor as supervisor_module

    payload = {
        "data_dir": str(tmp_path),
        "initialized": True,
        "postgres_port": 15432,
        "components": {"postgres": {"pid": 1, "alive": False, "port": 15432}},
    }
    monkeypatch.setattr(supervisor_module, "collect_status", lambda data_dir: payload)
    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path), "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["initialized"] is True
    assert parsed["components"]["postgres"]["port"] == 15432


def test_status_table_renders_components(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.supervisor as supervisor_module

    payload = {
        "data_dir": str(tmp_path),
        "initialized": True,
        "launcher": {"pid": 42, "mode": "serve", "alive": True},
        "components": {
            "postgres": {"pid": 100, "alive": True, "port": 15432},
            "redis": {"pid": None, "alive": False, "port": 16379},
        },
    }
    monkeypatch.setattr(supervisor_module, "collect_status", lambda data_dir: payload)
    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "postgres" in result.output
    assert "redis" in result.output
    assert "100" in result.output


def test_status_on_uninitialized_dir_does_not_crash(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "initialized: False" in result.output


def test_doctor_command_invokes_run_doctor_and_propagates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.cli.main as cli_main_module
    import modulo.launcher.doctor as doctor_module

    calls: list[tuple[Path, bool]] = []

    def fake_run_doctor(data_dir: Path, *, as_json: bool = False, probes=None) -> int:
        calls.append((data_dir, as_json))
        return 1

    monkeypatch.setattr(doctor_module, "run_doctor", fake_run_doctor)
    result = CliRunner().invoke(cli_main_module.cli, ["doctor", "--data-dir", str(tmp_path), "--json"])
    assert result.exit_code == 1
    assert calls == [(tmp_path, True)]


def test_doctor_command_render_runtime_error_as_click_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import modulo.cli.main as cli_main_module
    import modulo.launcher.doctor as doctor_module

    def fake_run_doctor(data_dir: Path, *, as_json: bool = False, probes=None) -> int:
        raise RuntimeError("data dir is not initialized")

    monkeypatch.setattr(doctor_module, "run_doctor", fake_run_doctor)
    result = CliRunner().invoke(cli_main_module.cli, ["doctor", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "data dir is not initialized" in result.output


def test_platform_guard_failure_degrades_status(tmp_path: Path) -> None:
    """On Windows (no launcher support) status still renders, without raising."""
    if sys.platform != "win32":
        pytest.skip("Windows-only degradation path")
    result = CliRunner().invoke(cli_main.cli, ["status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "initialized: False" in result.output


def test_doctor_command_invokes_run_doctor_and_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import modulo.launcher.doctor as doctor_module

    captured: dict[str, object] = {}

    def _fake_run_doctor(data_dir: Path, *, as_json: bool = False, probes=None) -> int:
        captured["data_dir"] = data_dir
        captured["as_json"] = as_json
        return 1

    monkeypatch.setattr(doctor_module, "run_doctor", _fake_run_doctor)
    monkeypatch.setattr(cli_main, "_resolve_data_dir", lambda data_dir: tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["doctor", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert captured["data_dir"] == tmp_path


def test_doctor_command_json_flag_passed_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import modulo.launcher.doctor as doctor_module

    captured: dict[str, object] = {}

    def _fake_run_doctor(data_dir: Path, *, as_json: bool = False, probes=None) -> int:
        captured["as_json"] = as_json
        return 0

    monkeypatch.setattr(doctor_module, "run_doctor", _fake_run_doctor)
    monkeypatch.setattr(cli_main, "_resolve_data_dir", lambda data_dir: tmp_path)

    result = CliRunner().invoke(cli_main.cli, ["doctor", "--data-dir", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert captured["as_json"] is True


def test_doctor_command_help_lists_options() -> None:
    result = CliRunner().invoke(cli_main.cli, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--data-dir" in result.output
    assert "--json" in result.output
