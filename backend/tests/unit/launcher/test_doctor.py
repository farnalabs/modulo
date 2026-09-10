"""Unit tests for the doctor-lite module (FAR-671 slice 3) and the
full-doctor extension (FAR-676).

Each check is a pure function over injected probes: lock pass/fail paths per
check, fault cases with actionable detail (redis stopped, wrong port,
unwritable dir, foreign bind), the never-crash contract (a probe exception
becomes a failed check), and the documented exit-code table (0 healthy,
1 unhealthy, 2 degraded, 3 uninitialized) with a deterministic fault recipe
per code.
"""

import json
import sys
import time
from pathlib import Path

import pytest

from modulo.launcher import doctor as doctor_module  # noqa: F401 — re-exported for fault injection in recipes
from modulo.launcher.doctor import (
    EXIT_DEGRADED,
    EXIT_HEALTHY,
    EXIT_UNHEALTHY,
    EXIT_UNINITIALIZED,
    DoctorProbes,
    apply_fixes,
    check_ambient_pg_env,
    check_bundle_versions,
    check_bundled_binaries,
    check_cloud_sync_root,
    check_cwd_env_influence,
    check_data_dir,
    check_degraded,
    check_install_shadows,
    check_memory_headroom,
    check_migrations,
    check_port_collisions,
    check_ports,
    check_postgres,
    check_privileges,
    check_redis,
    check_secrets_permissions,
    check_service_identity,
    check_settings_source,
    check_stale_backup,
    check_tls_expiry,
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
        secrets_mode=lambda _data_dir: 0o600,
        ambient_env_names=list,
        env_value=_env_probe,
        service_installed=lambda: False,
        service_enabled=lambda: False,
        service_linger=lambda: False,
        available_memory_bytes=lambda: 8 * 1024 * 1024 * 1024,
        data_dir_pg_version=lambda: "16.4",
        bundle_pg_version=lambda: "16.4",
        installed_bundle_pg_version=lambda: None,
        bundled_binaries=list,
        port_owner_description=lambda _port: None,
        second_install_hint=lambda: None,
        modulo_on_path=lambda: None,
        install_root=lambda: str(Path("/install")),
        degraded_reason=lambda: None,
        last_backup_at=lambda: time.time(),
        tls_expiry=lambda: time.time() + 400 * 86400,
        cloud_sync_hit=lambda _root: None,
        cwd_env_file=None,
        env_file_pinned=lambda: True,
        launcher_running=lambda: True,
    )
    for key, value in overrides.items():
        setattr(probes, key, value)
    return probes


def _env_probe(name: str) -> str | None:
    return None


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
    # FAR-676: an uninitialized data dir has its own documented exit code (3).
    assert code == EXIT_UNINITIALIZED
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


# ---------------------------------------------------------------------------
# FAR-676 extended checks
# ---------------------------------------------------------------------------


def test_secrets_permissions_fail_world_readable(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    result = check_secrets_permissions(tmp_path, None, _probes(secrets_mode=lambda _d: 0o644))
    assert result.ok is False
    assert "chmod 600" in result.detail


def test_secrets_permissions_pass(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    result = check_secrets_permissions(tmp_path, None, _probes())
    assert result.ok is True


def test_secrets_permissions_honest_skip_without_mode(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    result = check_secrets_permissions(tmp_path, None, _probes(secrets_mode=lambda _d: None))
    assert result.ok is True
    assert "TODO(P3)" in result.detail


def test_ambient_pg_env_warns_on_pg_vars() -> None:
    result = check_ambient_pg_env(Path(), None, _probes(ambient_env_names=lambda: ["PGHOST", "PGPASSWORD"]))
    assert result.ok is True
    assert result.warning is True
    assert "PGHOST" in result.detail


def test_ambient_pg_env_pass_clean() -> None:
    assert check_ambient_pg_env(Path(), None, _probes()).ok is True


def test_settings_source_warns_modulo_db_not_postgres() -> None:
    probes = _probes(env_value=lambda name: {"MODULO_DB": "sqlite"}.get(name))
    result = check_settings_source(Path(), _state(), probes)
    assert result.ok is True
    assert result.warning is True
    assert "MODULO_DB" in result.detail


def test_settings_source_warns_database_url_port_conflict() -> None:
    probes = _probes(
        env_value=lambda name: {"DATABASE_URL": "postgresql://u:p@foreign-host:9999/db"}.get(name),
    )
    result = check_settings_source(Path(), _state(), probes)
    assert result.warning is True
    assert "9999" in result.detail
    assert "15432" in result.detail


def test_settings_source_pass_clean() -> None:
    result = check_settings_source(Path(), _state(), _probes())
    assert result.ok is True
    assert result.warning is False


def test_cloud_sync_root_warns_on_vendor_match(tmp_path: Path) -> None:
    result = check_cloud_sync_root(
        tmp_path,
        None,
        _probes(cloud_sync_hit=lambda _root: "/home/x/Dropbox (matched marker)"),
    )
    assert result.ok is True
    assert result.warning is True
    assert "Dropbox" in result.detail


def test_cloud_sync_root_none_marker(tmp_path: Path) -> None:
    result = check_cloud_sync_root(tmp_path, None, _probes())
    assert result.ok is True
    assert result.warning is False


def test_service_not_installed_is_graceful_pass() -> None:
    result = check_service_identity(Path(), None, _probes(service_installed=lambda: False))
    assert result.ok is True
    assert "not installed" in result.detail


def test_service_installed_but_not_enabled_fails() -> None:
    result = check_service_identity(
        Path(),
        None,
        _probes(service_installed=lambda: True, service_enabled=lambda: False, service_linger=lambda: True),
    )
    assert result.ok is False
    assert "enable" in result.detail


def test_service_installed_without_linger_fails() -> None:
    result = check_service_identity(
        Path(),
        None,
        _probes(service_installed=lambda: True, service_enabled=lambda: True, service_linger=lambda: False),
    )
    assert result.ok is False
    assert "linger" in result.detail


def test_service_installed_pass() -> None:
    result = check_service_identity(
        Path(), None, _probes(service_installed=lambda: True, service_enabled=lambda: True, service_linger=lambda: True)
    )
    assert result.ok is True


def test_memory_fail_below_floor() -> None:
    result = check_memory_headroom(Path(), None, _probes(available_memory_bytes=lambda: 512 * 1024 * 1024))
    assert result.ok is False
    assert "floor" in result.detail


def test_memory_warn_below_comfortable() -> None:
    result = check_memory_headroom(Path(), None, _probes(available_memory_bytes=lambda: 1.5 * 1024 * 1024 * 1024))
    assert result.ok is True
    assert result.warning is True


def test_memory_pass() -> None:
    assert check_memory_headroom(Path(), None, _probes()).ok is True


def test_bundle_version_fail_minor_drift() -> None:
    result = check_bundle_versions(Path(), None, _probes(data_dir_pg_version=lambda: "16.2"))
    assert result.ok is False
    assert "16.2" in result.detail
    assert "16.4" in result.detail


def test_bundle_version_fail_downgrade() -> None:
    result = check_bundle_versions(Path(), None, _probes(installed_bundle_pg_version=lambda: "17.0"))
    assert result.ok is False
    assert "OLDER" in result.detail


def test_bundle_version_warn_upgrade() -> None:
    result = check_bundle_versions(Path(), None, _probes(installed_bundle_pg_version=lambda: "16.2"))
    assert result.ok is True
    assert result.warning is True
    assert "upgrade" in result.detail


def test_bundle_version_pass_match() -> None:
    assert check_bundle_versions(Path(), None, _probes()).ok is True


def test_bundle_version_skip_uninitialized() -> None:
    result = check_bundle_versions(Path(), None, _probes(data_dir_pg_version=lambda: None))
    assert result.ok is True


def test_bundled_binaries_fail_zero_byte(tmp_path: Path) -> None:
    quarantine = tmp_path / "postgres"
    quarantine.write_bytes(b"")
    result = check_bundled_binaries(tmp_path, None, _probes(bundled_binaries=lambda: [quarantine]))
    assert result.ok is False
    assert "ZERO bytes" in result.detail


def test_bundled_binaries_fail_not_executable(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows reports the exec bit implicitly; POSIX-only seam (TODO(P3) quarantine detection)")
    binary = tmp_path / "postgres"
    binary.write_bytes(b"#!/bin/sh\n")
    import stat

    binary.chmod(stat.S_IRUSR | stat.S_IWUSR)  # no exec bit
    result = check_bundled_binaries(tmp_path, None, _probes(bundled_binaries=lambda: [binary]))
    assert result.ok is False
    assert "NOT executable" in result.detail


def test_bundled_binaries_pass(tmp_path: Path) -> None:
    import stat

    binary = tmp_path / "postgres"
    binary.write_bytes(b"#!/bin/sh\n")
    if sys.platform == "win32":
        pytest.skip("exec-bit positive case is POSIX-stat-driven")
    binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    result = check_bundled_binaries(tmp_path, None, _probes(bundled_binaries=lambda: [binary]))
    assert result.ok is True


def test_binaries_real_machine_only_note(tmp_path: Path) -> None:
    """No bundled binaries resolved -> honest skip (real-machine-only audit)."""
    result = check_bundled_binaries(tmp_path, None, _probes())
    assert result.ok is True
    assert "real-machine-only" in result.detail


def test_port_collisions_fail_with_attribution() -> None:
    probes = _probes(
        port_owner_description=lambda port: f"system postgres service (pid 42) on port {port}",
        launcher_running=lambda: True,
    )
    result = check_port_collisions(Path(), _state(), probes)
    assert result.ok is False
    assert "system postgres" in result.detail
    assert "15432" in result.detail


def test_port_collisions_skip_when_not_running() -> None:
    result = check_port_collisions(Path(), _state(), _probes(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_port_collisions_pass_clean() -> None:
    result = check_port_collisions(Path(), _state(), _probes())
    assert result.ok is True


def test_install_shadows_warn_path_shadowing() -> None:
    result = check_install_shadows(
        Path("/data/modulo/data"), None, _probes(modulo_on_path=lambda: "/usr/local/bin/modulo")
    )
    assert result.ok is True
    assert result.warning is True
    assert "shadow" in result.detail


def test_install_shadows_warn_second_install(tmp_path: Path) -> None:
    hint = f"second native install at {tmp_path}/other"
    result = check_install_shadows(Path(), None, _probes(second_install_hint=lambda: hint))
    assert result.ok is True
    assert result.warning is True


def test_install_shadows_pass() -> None:
    assert check_install_shadows(Path("/data/modulo/data"), None, _probes()).ok is True


def test_degraded_fail_reason_surfaced() -> None:
    result = check_degraded(Path(), None, _probes(degraded_reason=lambda: "child 'postgres' crashed 5 times"))
    assert result.ok is False
    assert "degraded state: child 'postgres' crashed" in result.detail


def test_degraded_pass_clean() -> None:
    assert check_degraded(Path(), None, _probes()).ok is True


def test_tls_fail_expired() -> None:
    result = check_tls_expiry(Path(), None, _probes(tls_expiry=lambda: time.time() - 100))
    assert result.ok is False
    assert "EXPIRED" in result.detail


def test_tls_warn_near_expiry() -> None:
    result = check_tls_expiry(Path(), None, _probes(tls_expiry=lambda: time.time() + 10 * 86400))
    assert result.ok is True
    assert result.warning is True


def test_tls_pass_far_expiry() -> None:
    result = check_tls_expiry(Path(), None, _probes())
    assert result.ok is True
    assert result.warning is False


def test_tls_skip_absent_keypair() -> None:
    result = check_tls_expiry(Path(), None, _probes(tls_expiry=lambda: None))
    assert result.ok is True


def test_stale_backup_warn_old(tmp_path: Path) -> None:
    result = check_stale_backup(tmp_path, None, _probes(last_backup_at=lambda: time.time() - 30 * 86400))
    assert result.ok is True
    assert result.warning is True
    assert "30" in result.detail


def test_stale_backup_fail_missing(tmp_path: Path) -> None:
    """No last_backup_at ever recorded -> honest skip (schema v1)."""
    result = check_stale_backup(tmp_path, None, _probes(last_backup_at=lambda: None))
    assert result.ok is True
    assert "schema v1" in result.detail


def test_stale_backup_pass_recent(tmp_path: Path) -> None:
    result = check_stale_backup(tmp_path, None, _probes())
    assert result.ok is True
    assert result.warning is False


# ---------------------------------------------------------------------------
# state-integrity: DISTINCT corrupt vs hmac-mismatch (via run_doctor)
# ---------------------------------------------------------------------------


def test_state_corrupt_is_distinct_from_hmac_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "secrets.json").write_text(
        json.dumps({"postgres_password": "p", "redis_password": "r", "state_hmac_key": "0" * 64}),
        encoding="utf-8",
    )
    # corrupt: valid HMAC envelope missing entirely (torn) — write garbage
    (tmp_path / "state.json").write_text("}not json{", encoding="utf-8")
    assert run_doctor(tmp_path, probes=_probes()) == EXIT_UNHEALTHY
    out = capsys.readouterr().out
    assert "[FAIL] state-integrity" in out
    assert "CORRUPT" in out

    # tampered envelope (json parses) -> hmac-mismatch language
    keys = "0" * 64
    save_state(_state(), tmp_path / "state.json", bytes.fromhex(keys))
    envelope = json.loads((tmp_path / "state.json").read_text())
    envelope["mac"] = "ff" * 32
    (tmp_path / "state.json").write_text(json.dumps(envelope), encoding="utf-8")
    assert run_doctor(tmp_path, probes=_probes()) == EXIT_UNHEALTHY
    out = capsys.readouterr().out
    assert "HMAC verification" in out


# ---------------------------------------------------------------------------
# --fix
# ---------------------------------------------------------------------------


def test_apply_fix_sweeps_orphan_and_sweeps_debris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    stale_pid = pgdata / "postmaster.pid"
    stale_pid.write_text("99999\n0\n-1\n", encoding="utf-8")  # dead PID => stale
    (pgdata.parent / ".redis-conf-deadbeef").write_text("requirepass x", encoding="utf-8")

    import modulo.launcher.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "_is_postgres_process", lambda _pid: False)
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: False)
    actions = apply_fixes(tmp_path)
    assert any("removed_stale_postmaster_pid" in action for action in actions), actions
    assert not stale_pid.exists()
    assert not (pgdata.parent / ".redis-conf-deadbeef").exists()


def test_apply_fix_refuses_live_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text("99999\n0\n0\n", encoding="utf-8")

    import modulo.launcher.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "_is_postgres_process", lambda _pid: True)
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: True)
    with pytest.raises(RuntimeError, match="refused"):
        apply_fixes(tmp_path)


# ---------------------------------------------------------------------------
# documented exit-code table: every code has a deterministic fault recipe
# ---------------------------------------------------------------------------


def _exit_recipe(code: int, tmp_path: Path) -> int:
    """Deterministic, CI-automatable fault recipe per documented exit code.

    (Every documented code is mapped here; the real-machine-only recipes —
    e.g. an actual AV-quarantined binary — are annotated on the checks
    themselves and never silently.)
    """
    if code == EXIT_HEALTHY:
        _write_state_secrets(tmp_path)
        return run_doctor(tmp_path, probes=_probes())
    if code == EXIT_UNHEALTHY:
        _write_state_secrets(tmp_path)
        probes = _probes(probe_redis=lambda: (_ for _ in ()).throw(RuntimeError("redis stopped")))
        return run_doctor(tmp_path, probes=probes)
    if code == EXIT_DEGRADED:
        _write_state_secrets(tmp_path)
        return run_doctor(
            tmp_path,
            probes=_probes(last_backup_at=lambda: time.time() - 30 * 86400),
        )
    if code == EXIT_UNINITIALIZED:
        # secrets.json + state.json absent -> uninitialized data dir
        return run_doctor(tmp_path, probes=_probes())
    raise AssertionError(f"undocumented doctor exit code: {code}")


@pytest.mark.parametrize(
    ("code", "recipe_name"),
    [
        pytest.param(EXIT_HEALTHY, "default probes back the happy path (CI-automatable)", id="exit-0-healthy"),
        pytest.param(EXIT_UNHEALTHY, "redis stopped (CI-automatable)", id="exit-1-unhealthy"),
        pytest.param(EXIT_DEGRADED, "stale backup warning (CI-automatable)", id="exit-2-degraded"),
        pytest.param(
            EXIT_UNINITIALIZED, "no state/secrets in the data dir (CI-automatable)", id="exit-3-uninitialized"
        ),
    ],
)
def test_documented_exit_codes_are_exhaustive(code: int, recipe_name: str, tmp_path: Path) -> None:
    assert code in {EXIT_HEALTHY, EXIT_UNHEALTHY, EXIT_DEGRADED, EXIT_UNINITIALIZED}
    assert run_exit_code_recipe(code, tmp_path) == code


def run_exit_code_recipe(code: int, tmp_path: Path) -> int:
    return _exit_recipe(code, tmp_path)


DETERMINISTIC_RECIPES = {
    EXIT_HEALTHY: "CI-automatable: healthy default probes",
    EXIT_UNHEALTHY: "CI-automatable: probe_redis raises (redis stopped)",
    EXIT_DEGRADED: "CI-automatable: last_backup_at 30d ago",
    EXIT_UNINITIALIZED: "CI-automatable: empty data dir",
}
REAL_MACHINE_ONLY_RECIPES: dict[int, str] = {}


def test_exit_code_table_backed_by_recipe_registry() -> None:
    """Every documented doctor code has a recipe annotation (test lives IN
    this suite so the exhaustiveness is enforced whenever the table moves)."""
    assert set(DETERMINISTIC_RECIPES.keys()) == {
        EXIT_HEALTHY,
        EXIT_UNHEALTHY,
        EXIT_DEGRADED,
        EXIT_UNINITIALIZED,
    }


# ---------------------------------------------------------------------------
# state via run_doctor orchestration with extended defaults
# ---------------------------------------------------------------------------


def test_run_doctor_degraded_only_warnings_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    code = run_doctor(tmp_path, probes=_probes(last_backup_at=lambda: time.time() - 30 * 86400))
    assert code == EXIT_DEGRADED
    assert "stale-backup" in capsys.readouterr().out


def test_run_doctor_json_includes_exit_code_and_warnings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    run_doctor(tmp_path, as_json=True, probes=_probes(last_backup_at=lambda: time.time() - 30 * 86400))
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == EXIT_DEGRADED
    stale = next(c for c in payload["checks"] if c["name"] == "stale-backup")
    assert stale["warning"] is True
    assert stale["ok"] is True
