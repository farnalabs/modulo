"""Unit tests for the doctor-lite module (FAR-671 slice 3).

Each check is a pure function over injected probes: lock pass/fail paths per
check, fault cases with actionable detail (redis stopped, wrong port,
unwritable dir, foreign bind), the never-crash contract (a probe exception
becomes a failed check), and the 0/1 exit-code convention.
"""

import json
from pathlib import Path

import pytest

from modulo.launcher.doctor import (
    DoctorProbes,
    check_cwd_env_influence,
    check_data_dir,
    check_migrations,
    check_ports,
    check_postgres,
    check_privileges,
    check_redis,
    run_doctor,
)
from modulo.launcher.secrets_file import LauncherSecrets, _parse
from modulo.launcher.state import LauncherState, load_state, save_state


def _probes(**overrides: object) -> DoctorProbes:
    probes = DoctorProbes(
        disk_free_bytes=lambda _root: 40 * 1024 * 1024 * 1024,
        assert_writable=lambda _root: None,
        listening_on=lambda _port: ["127.0.0.1"],
        probe_postgres=lambda: None,
        role_violations=list,
        probe_redis=lambda: None,
        migrations_at_head=lambda: True,
        effective_uid=lambda: 1000,
        username_of_uid=lambda _uid: "operator",
        file_owner=lambda _path: "operator",
        cwd_env_file=None,
        env_file_pinned=lambda: True,
        launcher_running=lambda: True,
    )
    for key, value in overrides.items():
        setattr(probes, key, value)
    return probes


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


def _write_state_secrets(tmp_path: Path) -> LauncherSecrets:
    secrets = LauncherSecrets(postgres_password="pg-pw", redis_password="redis-pw", state_hmac_key=bytes(range(32)))
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "postgres_password": secrets.postgres_password,
                "redis_password": secrets.redis_password,
                "state_hmac_key": secrets.state_hmac_key_hex,
            }
        ),
        encoding="utf-8",
    )
    save_state(_state(), tmp_path / "state.json", secrets.state_hmac_key)
    return secrets


# ---------------------------------------------------------------------------
# check 1: data dir
# ---------------------------------------------------------------------------


def test_data_dir_pass_reports_disk_and_writability(tmp_path: Path) -> None:
    result = check_data_dir(tmp_path, _state(), _probes())
    assert result.ok is True
    assert "GiB free" in result.detail


def test_data_dir_fail_below_disk_floor(tmp_path: Path) -> None:
    result = check_data_dir(tmp_path, _state(), _probes(disk_free_bytes=lambda _root: 128 * 1024 * 1024))
    assert result.ok is False
    assert "MiB free" in result.detail
    assert "floor" in result.detail


def test_data_dir_fail_unwritable_dir(tmp_path: Path) -> None:
    def refuse(_root: Path) -> None:
        raise OSError("data dir is not writable by this user")

    result = check_data_dir(tmp_path, _state(), _probes(assert_writable=refuse))
    assert result.ok is False
    assert "writable" in result.detail


# ---------------------------------------------------------------------------
# check 2: ports + loopback bind
# ---------------------------------------------------------------------------


def test_ports_fail_state_unreadable() -> None:
    result = check_ports(Path("/uninitialized"), None, _probes())
    assert result.ok is False
    assert "state.json" in result.detail


def test_ports_skipped_when_launcher_not_running(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_ports_fail_missing_listeners(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes(listening_on=lambda _port: []))
    assert result.ok is False
    assert "postgres is NOT listening on configured port 15432" in result.detail


def test_ports_fail_redis_wrong_port(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes(listening_on=lambda _port: []))
    assert result.ok is False
    assert "redis is NOT listening on configured port 16379" in result.detail


def test_ports_fail_foreign_loopback_binding(tmp_path: Path) -> None:
    foreign_bind = ["0.0.0.0"]  # noqa: S104 — simulated foreign bind

    result = check_ports(tmp_path, _state(), _probes(listening_on=lambda _port: foreign_bind))
    assert result.ok is False
    assert "bound outside loopback" in result.detail


def test_ports_pass_loopback_listeners(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 3: postgres reachable + role posture
# ---------------------------------------------------------------------------


def test_postgres_skipped_when_launcher_not_running() -> None:
    result = check_postgres(Path(), _state(), _probes(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_postgres_fail_service_down_with_detail() -> None:
    def down() -> None:
        raise RuntimeError("connection refused")

    result = check_postgres(Path(), _state(), _probes(probe_postgres=down))
    assert result.ok is False
    assert "connection refused" in result.detail


def test_postgres_fail_role_posture_violations() -> None:
    result = check_postgres(Path(), _state(), _probes(role_violations=lambda: ["app role modulo_app has BYPASSRLS"]))
    assert result.ok is False
    assert "posture" in result.detail
    assert "BYPASSRLS" in result.detail


def test_postgres_pass_posture_holds() -> None:
    result = check_postgres(Path(), _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 4: redis ping + auth
# ---------------------------------------------------------------------------


def test_redis_fail_when_stopped() -> None:
    def stopped() -> None:
        raise RuntimeError("Connection refused")

    result = check_redis(Path(), _state(), _probes(probe_redis=stopped))
    assert result.ok is False
    assert "Connection refused" in result.detail


def test_redis_fail_bad_auth() -> None:
    def bad_auth() -> None:
        raise RuntimeError("NOAUTH Authentication required")

    result = check_redis(Path(), _state(), _probes(probe_redis=bad_auth))
    assert result.ok is False
    assert "NOAUTH" in result.detail


def test_redis_pass() -> None:
    result = check_redis(Path(), _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 5: migrations
# ---------------------------------------------------------------------------


def test_migrations_fail_behind_head() -> None:
    result = check_migrations(Path(), _state(), _probes(migrations_at_head=lambda: False))
    assert result.ok is False
    assert "alembic head" in result.detail


def test_migrations_pass() -> None:
    result = check_migrations(Path(), _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 6: CWD .env influence refusal
# ---------------------------------------------------------------------------


def test_env_influence_pass_no_cwd_env() -> None:
    result = check_cwd_env_influence(Path(), _state(), _probes(cwd_env_file=None))
    assert result.ok is True


def test_env_influence_fail_ambient_urls_unpinned(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://foreign/db", encoding="utf-8")
    result = check_cwd_env_influence(tmp_path, _state(), _probes(cwd_env_file=env_file, env_file_pinned=lambda: False))
    assert result.ok is False
    assert "DATABASE_URL" in result.detail


def test_env_influence_pass_ambient_urls_pinned(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://foreign/db", encoding="utf-8")
    result = check_cwd_env_influence(tmp_path, _state(), _probes(cwd_env_file=env_file, env_file_pinned=lambda: True))
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 7: privileges
# ---------------------------------------------------------------------------


def test_privileges_fail_root(tmp_path: Path) -> None:
    result = check_privileges(tmp_path, _state(), _probes(effective_uid=lambda: 0, username_of_uid=lambda _uid: "root"))
    assert result.ok is False
    assert "root" in result.detail


def test_privileges_fail_ownership_mismatch(tmp_path: Path) -> None:
    result = check_privileges(
        tmp_path,
        _state(),
        _probes(effective_uid=lambda: 1000, username_of_uid=lambda _uid: "operator", file_owner=lambda _p: "root"),
    )
    assert result.ok is False
    assert "owned by 'root'" in result.detail


def test_privileges_pass_unprivileged_consistent(tmp_path: Path) -> None:
    result = check_privileges(tmp_path, _state(), _probes())
    assert result.ok is True


def test_privileges_honest_skip_without_uid(tmp_path: Path) -> None:
    result = check_privileges(tmp_path, _state(), _probes(effective_uid=lambda: None))
    assert result.ok is True
    assert "TODO(P3)" in result.detail


# ---------------------------------------------------------------------------
# run_doctor orchestration: exit codes, never-crash, JSON
# ---------------------------------------------------------------------------


def test_state_secrets_roundtrip(tmp_path: Path) -> None:
    secrets = _write_state_secrets(tmp_path)
    loaded = load_state(tmp_path / "state.json", secrets.state_hmac_key)
    assert loaded.postgres_port == _state().postgres_port
    parsed = _parse((tmp_path / "secrets.json").read_bytes())
    assert isinstance(parsed.state_hmac_key, bytes)


def test_run_doctor_healthy_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    code = run_doctor(tmp_path, probes=_probes())
    assert code == 0
    assert "healthy: all checks passed" in capsys.readouterr().out


def test_run_doctor_failing_check_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    code = run_doctor(
        tmp_path,
        probes=_probes(role_violations=lambda: ["app role modulo_app has BYPASSRLS"]),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "unhealthy" in out
    assert "postgres" in out


def test_run_doctor_state_unavailable_reports_distinct_failed_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_doctor(tmp_path, probes=_probes())
    assert code == 1
    out = capsys.readouterr().out
    for name in ("ports", "postgres", "redis", "migrations"):
        assert f"[FAIL] {name}" in out
    assert "[ok  ] data-dir" in out


def test_run_doctor_crashing_probe_becomes_failed_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)

    def exploding_disk(_root: Path) -> int:
        raise ValueError("probe went sideways")

    code = run_doctor(tmp_path, probes=_probes(disk_free_bytes=exploding_disk))
    assert code == 1
    out = capsys.readouterr().out
    assert "probe went sideways" in out
    assert "[FAIL] data-dir" in out


def test_run_doctor_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    run_doctor(tmp_path, as_json=True, probes=_probes())
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["data_dir"] == str(tmp_path)
    names = {item["name"] for item in payload["checks"]}
    assert names >= {"data-dir", "ports", "postgres", "redis", "migrations"}


def test_run_doctor_never_raises_on_any_crash(tmp_path: Path) -> None:
    _write_state_secrets(tmp_path)

    def crashing_disk(_root: Path) -> int:
        raise RuntimeError("disk probe crashed")

    def stopped_redis() -> None:
        raise RuntimeError("redis stopped")

    code = run_doctor(
        tmp_path,
        probes=_probes(disk_free_bytes=crashing_disk, probe_redis=stopped_redis),
    )
    assert code == 1
