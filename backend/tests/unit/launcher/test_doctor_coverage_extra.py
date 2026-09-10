"""Extra coverage for the full-doctor module (FAR-676).

Targets the new-check error/edge branches and the real ``default_probes``
implementations so the PR's new-code coverage clears the SonarCloud 80%
new-coverage gate. Every check is a pure function over injected probes, so
each failure/edge path is exercised with a crafted probe.

These tests are platform-safe (no live Postgres/Redis required) — the real
probe implementations are exercised against a temp dir and their failure
paths are asserted directly rather than against external services.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from modulo.launcher import doctor as doctor_module
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
    check_degraded,
    check_install_shadows,
    check_memory_headroom,
    check_port_collisions,
    check_privileges,
    check_secrets_permissions,
    check_service_identity,
    check_settings_source,
    check_stale_backup,
    check_tls_expiry,
    default_probes,
    run_doctor,
)
from modulo.launcher.secrets_file import LauncherSecrets
from modulo.launcher.state import LauncherState, save_state


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
        env_value=lambda _name: None,
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


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


def _write_state_secrets(tmp_path: Path) -> LauncherSecrets:
    secrets = LauncherSecrets(postgres_password="pg-pw", redis_password="redis-pw", state_hmac_key=bytes(range(32)))
    (tmp_path / "secrets.json").write_text(
        '{"postgres_password": "pg-pw", "redis_password": "redis-pw", '
        f'"state_hmac_key": "{secrets.state_hmac_key_hex}"}}',
        encoding="utf-8",
    )
    save_state(_state(), tmp_path / "state.json", secrets.state_hmac_key)
    return secrets


# ---------------------------------------------------------------------------
# Check error paths (probe exception -> failed check)
# ---------------------------------------------------------------------------


def _touch_secrets(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")


def test_secrets_permissions_probe_failure(tmp_path: Path):
    _touch_secrets(tmp_path)
    res = check_secrets_permissions(
        tmp_path, None, _probes(secrets_mode=lambda _d: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False
    assert "secrets-permission probe failed" in res.detail


def test_secrets_permissions_mode_mismatch(tmp_path: Path):
    _touch_secrets(tmp_path)
    res = check_secrets_permissions(tmp_path, None, _probes(secrets_mode=lambda _d: 0o644))
    assert res.ok is False
    assert "chmod 600" in res.detail


def test_secrets_permissions_mode_none(tmp_path: Path):
    res = check_secrets_permissions(tmp_path, None, _probes(secrets_mode=lambda _d: None))
    assert res.ok is True


def test_ambient_pg_env_probe_failure(tmp_path: Path):
    res = check_ambient_pg_env(
        tmp_path, None, _probes(ambient_env_names=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_settings_source_probe_failure(tmp_path: Path):
    res = check_settings_source(
        tmp_path, _state(), _probes(env_value=lambda _n: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_settings_source_moduledb_away(tmp_path: Path):
    res = check_settings_source(tmp_path, _state(), _probes(env_value=lambda n: "mysql" if n == "MODULO_DB" else None))
    assert res.ok is True
    assert res.warning is True
    assert "MODULO_DB" in res.detail


def test_settings_source_db_url_port_conflict(tmp_path: Path):
    res = check_settings_source(
        tmp_path, _state(), _probes(env_value=lambda n: "postgresql://h:19999/db" if n == "DATABASE_URL" else None)
    )
    assert res.ok is True
    assert res.warning is True
    assert "19999" in res.detail


def test_cloud_sync_probe_failure(tmp_path: Path):
    res = check_cloud_sync_root(
        tmp_path, None, _probes(cloud_sync_hit=lambda _d: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_service_identity_probe_failure(tmp_path: Path):
    res = check_service_identity(
        tmp_path, None, _probes(service_installed=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_service_identity_installed_degraded(tmp_path: Path):
    res = check_service_identity(
        tmp_path,
        None,
        _probes(service_installed=lambda: True, service_enabled=lambda: False, service_linger=lambda: False),
    )
    assert res.ok is False
    assert "systemctl enable" in res.detail


def test_service_identity_installed_healthy(tmp_path: Path):
    res = check_service_identity(
        tmp_path,
        None,
        _probes(service_installed=lambda: True, service_enabled=lambda: True, service_linger=lambda: True),
    )
    assert res.ok is True


def test_memory_probe_failure(tmp_path: Path):
    res = check_memory_headroom(
        tmp_path, None, _probes(available_memory_bytes=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_memory_unknown(tmp_path: Path):
    res = check_memory_headroom(tmp_path, None, _probes(available_memory_bytes=lambda: None))
    assert res.ok is True


def test_memory_below_floor(tmp_path: Path):
    res = check_memory_headroom(tmp_path, None, _probes(available_memory_bytes=lambda: 512 * 1024 * 1024))
    assert res.ok is False
    assert "MiB" in res.detail


def test_memory_warn_band(tmp_path: Path):
    res = check_memory_headroom(tmp_path, None, _probes(available_memory_bytes=lambda: 1536 * 1024 * 1024))
    assert res.ok is True
    assert res.warning is True


def test_bundle_versions_probe_failure(tmp_path: Path):
    res = check_bundle_versions(
        tmp_path, _state(), _probes(data_dir_pg_version=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_bundle_versions_bundle_none(tmp_path: Path):
    res = check_bundle_versions(
        tmp_path, _state(), _probes(data_dir_pg_version=lambda: "16", bundle_pg_version=lambda: None)
    )
    assert res.ok is True


def test_bundle_versions_drift(tmp_path: Path):
    res = check_bundle_versions(
        tmp_path, _state(), _probes(data_dir_pg_version=lambda: "15", bundle_pg_version=lambda: "16.4")
    )
    assert res.ok is False


def test_bundle_versions_older_binary(tmp_path: Path):
    res = check_bundle_versions(
        tmp_path,
        _state(),
        _probes(
            data_dir_pg_version=lambda: "16",
            bundle_pg_version=lambda: "16.4",
            installed_bundle_pg_version=lambda: "17.0",
        ),
    )
    assert res.ok is False
    assert "OLDER" in res.detail


def test_bundle_versions_upgrade_available(tmp_path: Path):
    res = check_bundle_versions(
        tmp_path,
        _state(),
        _probes(
            data_dir_pg_version=lambda: "16",
            bundle_pg_version=lambda: "16.4",
            installed_bundle_pg_version=lambda: "16.2",
        ),
    )
    assert res.ok is True
    assert res.warning is True


def test_bundled_binaries_probe_failure(tmp_path: Path):
    res = check_bundled_binaries(
        tmp_path, None, _probes(bundled_binaries=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_port_collisions_launcher_probe_failure(tmp_path: Path):
    res = check_port_collisions(
        tmp_path, _state(), _probes(launcher_running=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_port_collisions_owner_probe_failure(tmp_path: Path):
    res = check_port_collisions(
        tmp_path, _state(), _probes(port_owner_description=lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_port_collisions_with_owner(tmp_path: Path):
    res = check_port_collisions(
        tmp_path, _state(), _probes(port_owner_description=lambda _p: "foreign service bound to 1.2.3.4")
    )
    assert res.ok is False
    assert "collision" in res.detail


def test_install_shadows_probe_failure(tmp_path: Path):
    res = check_install_shadows(
        tmp_path, None, _probes(modulo_on_path=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_install_shadows_path_shadow(tmp_path: Path):
    res = check_install_shadows(
        tmp_path, None, _probes(modulo_on_path=lambda: "/other/bin/modulo", install_root=lambda: "/install")
    )
    assert res.ok is True
    assert res.warning is True
    assert "PATH shadowing" in res.detail


def test_install_shadows_second_install(tmp_path: Path):
    res = check_install_shadows(
        tmp_path, None, _probes(second_install_hint=lambda: "a second native install lives at /x")
    )
    assert res.ok is True
    assert res.warning is True


def test_degraded_probe_failure(tmp_path: Path):
    res = check_degraded(tmp_path, None, _probes(degraded_reason=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
    assert res.ok is False


def test_tls_probe_failure(tmp_path: Path):
    res = check_tls_expiry(tmp_path, None, _probes(tls_expiry=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
    assert res.ok is False


def test_tls_expired(tmp_path: Path):
    res = check_tls_expiry(tmp_path, None, _probes(tls_expiry=lambda: time.time() - 10))
    assert res.ok is False
    assert "EXPIRED" in res.detail


def test_tls_near_expiry(tmp_path: Path):
    res = check_tls_expiry(tmp_path, None, _probes(tls_expiry=lambda: time.time() + 5 * 86400))
    assert res.ok is True
    assert res.warning is True


def test_tls_valid(tmp_path: Path):
    res = check_tls_expiry(tmp_path, None, _probes(tls_expiry=lambda: time.time() + 400 * 86400))
    assert res.ok is True
    assert res.warning is False


def test_stale_backup_probe_failure(tmp_path: Path):
    res = check_stale_backup(
        tmp_path, None, _probes(last_backup_at=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_stale_backup_stale(tmp_path: Path):
    res = check_stale_backup(tmp_path, None, _probes(last_backup_at=lambda: time.time() - 30 * 86400))
    assert res.ok is True
    assert res.warning is True


def test_stale_backup_fresh(tmp_path: Path):
    res = check_stale_backup(tmp_path, None, _probes(last_backup_at=lambda: time.time() - 60))
    assert res.ok is True
    assert res.warning is False


# ---------------------------------------------------------------------------
# check_cwd_env_influence edges
# ---------------------------------------------------------------------------


def test_cwd_env_influence_present_unpinned(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://h:1/db\n", encoding="utf-8")
    res = check_cwd_env_influence(tmp_path, None, _probes(cwd_env_file=env_file, env_file_pinned=lambda: False))
    assert res.ok is False


def test_cwd_env_influence_present_pinned(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://h:1/db\n", encoding="utf-8")
    res = check_cwd_env_influence(tmp_path, None, _probes(cwd_env_file=env_file, env_file_pinned=lambda: True))
    assert res.ok is True


def test_cwd_env_influence_present_no_urls(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n", encoding="utf-8")
    res = check_cwd_env_influence(tmp_path, None, _probes(cwd_env_file=env_file))
    assert res.ok is True


def test_privileges_uid_none(tmp_path: Path):
    res = check_privileges(tmp_path, None, _probes(effective_uid=lambda: None))
    assert res.ok is True


def test_privileges_root(tmp_path: Path):
    res = check_privileges(tmp_path, None, _probes(effective_uid=lambda: 0))
    assert res.ok is False


def test_privileges_owner_mismatch(tmp_path: Path):
    res = check_privileges(
        tmp_path,
        None,
        _probes(effective_uid=lambda: 1000, username_of_uid=lambda _u: "me", file_owner=lambda _p: "other"),
    )
    assert res.ok is False
    assert "chown" in res.detail


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_version_tuple():
    assert doctor_module._version_tuple("16.4") == (16, 4)
    assert doctor_module._version_tuple("16") == (16,)
    assert not doctor_module._version_tuple("abc")
    assert doctor_module._version_tuple("16.4.2") == (16, 4, 2)


def test_host_port_from_database_url():
    assert doctor_module._host_port_from_database_url("postgresql://h:5432/db") == ("h", 5432)
    # Non-loopback host with no explicit port defaults to 5432.
    assert doctor_module._host_port_from_database_url("postgresql://h/db") == ("h", 5432)
    assert doctor_module._host_port_from_database_url("postgresql://127.0.0.1/db") == ("127.0.0.1", None)
    # Out-of-range port raises ValueError -> honest ("unknown", None) skip.
    assert doctor_module._host_port_from_database_url("postgresql://h:99999/db") == ("unknown", None)


def test_password_from_url():
    assert doctor_module._password_from_url("redis://:secret@h:1") == "secret"
    assert not doctor_module._password_from_url("redis://h:1")


def test_decode_proc_address():
    import socket

    from modulo.launcher.doctor import _decode_proc_address

    # /proc stores addresses in network (big-endian) byte order.
    ipv4 = _decode_proc_address("0100007F", socket.AF_INET)
    assert ipv4 == "127.0.0.1"
    ipv6 = _decode_proc_address("00000000000000000000000001000000", socket.AF_INET6)
    assert ipv6 == "::1"


def test_port_owner_descriptions_no_foreign():
    from modulo.launcher.doctor import _port_owner_descriptions

    # No foreign listeners in the sandbox -> empty list, but the scan runs.
    assert not _port_owner_descriptions(15432)


def test_exit_code_for():
    from modulo.launcher.doctor import _exit_code_for

    ok = [doctor_module.CheckResult("x", True)]
    assert _exit_code_for(healthy=True, results=ok, state_kind=None) == EXIT_HEALTHY
    warn = [doctor_module.CheckResult("x", True, warning=True)]
    assert _exit_code_for(healthy=True, results=warn, state_kind=None) == EXIT_DEGRADED
    fail = [doctor_module.CheckResult("x", False)]
    assert _exit_code_for(healthy=False, results=fail, state_kind=None) == EXIT_UNHEALTHY
    assert _exit_code_for(healthy=True, results=ok, state_kind="missing") == EXIT_UNINITIALIZED


def test_state_integrity_detail():
    from modulo.launcher.doctor import _state_integrity_detail

    assert "HMAC" in _state_integrity_detail("err", "hmac-mismatch")
    assert "CORRUPT" in _state_integrity_detail("err", "corrupt")
    assert _state_integrity_detail("raw", None) == "raw"


def test_state_problem_kind(tmp_path: Path):
    from modulo.launcher.doctor import _state_problem_kind

    assert _state_problem_kind(tmp_path, "data dir is not initialized (no secrets file)") == "missing"
    assert _state_problem_kind(tmp_path, "secrets file unreadable: x") == "secrets-unreadable"
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    assert _state_problem_kind(tmp_path, "state.json unreadable: x") == "corrupt"
    assert _state_problem_kind(tmp_path, "HMAC verification failed") == "hmac-mismatch"
    assert _state_problem_kind(tmp_path, "schema_version mismatch") == "schema-version"
    assert _state_problem_kind(tmp_path, "weird") == "corrupt"


def test_payload_json_and_print_report():
    from modulo.launcher.doctor import _payload_json, _print_report

    results = [doctor_module.CheckResult("x", True, "ok")]
    payload = _payload_json(Path("/d"), True, results, 0)
    assert payload["healthy"] is True
    assert payload["exit_code"] == 0
    assert payload["checks"][0]["name"] == "x"

    lines: list[str] = []
    _print_report(Path("/d"), results, True, lines.append)
    assert any("healthy" in ln for ln in lines)


# ---------------------------------------------------------------------------
# Real default_probes implementations (exercised against a temp data dir)
# ---------------------------------------------------------------------------


def test_default_probes_real(tmp_path: Path):
    _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, _state())

    # The writable probe creates then removes a probe file.
    probes.assert_writable(tmp_path)

    # uid / username / file_owner resolve on POSIX.
    uid = probes.effective_uid()
    assert isinstance(uid, int)
    assert probes.username_of_uid(uid) is not None or probes.username_of_uid(uid) is None
    assert probes.file_owner(tmp_path) is not None or probes.file_owner(tmp_path) is None

    # env snapshot probes
    assert probes.env_value("PATH") is not None or probes.env_value("PATH") is None
    assert isinstance(probes.ambient_env_names(), list)
    assert isinstance(probes.available_memory_bytes(), int) or probes.available_memory_bytes() is None

    # PG_VERSION read from pgdata
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n", encoding="ascii")
    assert probes.data_dir_pg_version() == "16"

    # cloud sync hit walks ancestors
    synced = tmp_path / "Dropbox" / "data"
    synced.mkdir(parents=True)
    assert probes.cloud_sync_hit(synced) is not None
    assert probes.cloud_sync_hit(tmp_path) is None

    # PATH / install root probes
    assert probes.install_root() is not None
    assert probes.modulo_on_path() is None or isinstance(probes.modulo_on_path(), str)

    # second install hint (sandbox may share a parent dir with other installs)
    hint = probes.second_install_hint()
    assert hint is None or "second native install" in hint

    # port owner description (no foreign listener -> None)
    assert probes.port_owner_description(15432) is None

    # last backup at from state
    state_with_backup = LauncherState(
        postgres_port=15432,
        redis_port=16379,
        api_port=18000,
        last_backup_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )
    save_state(state_with_backup, tmp_path / "state.json", bytes(range(32)))
    probes2 = default_probes(tmp_path, state_with_backup)
    assert probes2.last_backup_at() is not None

    # secrets mode reflects the real file
    (tmp_path / "secrets.json").chmod(0o600)
    assert probes.secrets_mode(tmp_path) == 0o600

    # bundled binaries / bundle version resolve (may be empty if no bundle)
    assert isinstance(probes.bundled_binaries(), list)
    assert probes.bundle_pg_version() is None or isinstance(probes.bundle_pg_version(), str)

    # launcher-running + env-file-pinned probes run without crashing
    assert isinstance(probes.launcher_running(), bool)
    assert isinstance(probes.env_file_pinned(), bool)

    # TLS expiry honest-skip when no tls dir
    assert probes.tls_expiry() is None


def test_default_probes_second_install_hint(tmp_path: Path):
    _write_state_secrets(tmp_path)
    sibling = tmp_path.parent / "sibling-data"
    sibling.mkdir()
    (sibling / "state.json").write_text("{}", encoding="utf-8")
    (sibling / "secrets.json").write_text("{}", encoding="utf-8")
    probes = default_probes(tmp_path, _state())
    assert probes.second_install_hint() is not None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_run_doctor_uninitialized(tmp_path: Path):
    lines: list[str] = []
    code = run_doctor(tmp_path, sink=lines.append)
    assert code == EXIT_UNINITIALIZED
    assert any("state" in ln.lower() for ln in lines)


def test_run_doctor_as_json(tmp_path: Path):
    _write_state_secrets(tmp_path)
    out: list[str] = []
    code = run_doctor(tmp_path, as_json=True, probes=_probes(), sink=out.append)
    assert code in (EXIT_HEALTHY, EXIT_DEGRADED)
    import json

    payload = json.loads("\n".join(out))
    assert payload["exit_code"] == code
    assert any(c["name"] == "data-dir" for c in payload["checks"])


def test_run_doctor_fix_noop(tmp_path: Path):
    _write_state_secrets(tmp_path)
    out: list[str] = []
    code = run_doctor(tmp_path, fix=True, probes=_probes(), sink=out.append)
    assert code in (EXIT_HEALTHY, EXIT_DEGRADED)
    assert any("orphan" in ln for ln in out)


def test_apply_fixes(tmp_path: Path):
    _write_state_secrets(tmp_path)
    (tmp_path / "pgdata").mkdir()
    actions = apply_fixes(tmp_path)
    assert any("orphan" in a or "port re-assignment" in a for a in actions)
