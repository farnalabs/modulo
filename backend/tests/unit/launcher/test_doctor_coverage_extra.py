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

import os
import shutil
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from tests.unit.launcher.test_launcher_shared import all_pass_probes


def _probes(**overrides: object) -> DoctorProbes:
    """The all-pass injection — the behavior-identical canonical factory
    lives in the shared launcher-test module."""
    return all_pass_probes(**overrides)


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


def test_memory_probe_failure(tmp_path: Path):
    res = check_memory_headroom(
        tmp_path, None, _probes(available_memory_bytes=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert res.ok is False


def test_memory_unknown(tmp_path: Path):
    res = check_memory_headroom(tmp_path, None, _probes(available_memory_bytes=lambda: None))
    assert res.ok is True


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


def _selfsigned_cert(lifetime_seconds: float) -> tuple[object, bytes]:
    """A real self-signed certificate (Ed25519) valid for ~lifetime_seconds.

    Returns (cert, PEM bytes) so tests can assert against the exact
    not_valid_after_utc the production probe will read.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.x509.oid import NameOID

    key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "modulo-tls-test")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(seconds=lifetime_seconds))
    )
    cert = builder.sign(key, algorithm=None)
    return cert, cert.public_bytes(serialization.Encoding.PEM)


def test_tls_keypair_expiry_earliest_cert_wins(tmp_path: Path):
    """A real keypair in <data_dir>/tls drives the expiry; the EARLIEST
    notAfter across the PEM chain (incl. concatenated chain files) wins."""
    tls_dir = tmp_path / "tls"
    tls_dir.mkdir()
    short_cert, short_pem = _selfsigned_cert(10 * 86400)
    _, long_pem = _selfsigned_cert(400 * 86400)
    (tls_dir / "chain.pem").write_bytes(short_pem + long_pem)
    (tls_dir / "server.pem").write_bytes(long_pem)

    expiry = doctor_module._tls_keypair_expiry(tmp_path)

    expected = short_cert.not_valid_after_utc.timestamp()
    assert expiry is not None
    assert abs(expiry - expected) < 5


def test_tls_keypair_expiry_absent_or_unparseable_is_none(tmp_path: Path):
    """No tls dir, or an unparseable PEM file, honestly returns None."""
    assert doctor_module._tls_keypair_expiry(tmp_path) is None

    garbage_dir = tmp_path / "tls-garbage"
    (garbage_dir / "tls").mkdir(parents=True)
    (garbage_dir / "tls" / "server.pem").write_bytes(b"not a certificate")
    (garbage_dir / "tls" / "nested_dir").mkdir()  # non-file entry is skipped
    assert doctor_module._tls_keypair_expiry(garbage_dir) is None


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

    # uid resolves to an int on POSIX and to None on Windows (os.getuid is
    # POSIX-only; _effective_uid's contract is `int | None`).
    uid = probes.effective_uid()
    if sys.platform == "win32":
        # TODO(P3): Windows seams; uid is None, and the uid/owner probes
        # honestly report None rather than guessing a SID/account.
        assert uid is None
        assert probes.username_of_uid(1000) is None
        assert probes.file_owner(tmp_path) is None
    else:
        assert isinstance(uid, int)
        import pwd

        # Both probes resolve to THIS process's own login name on POSIX.
        # Guard the UID lookup: a runner whose UID has no /etc/passwd entry
        # makes getpwuid raise KeyError, whereas the probe under test returns
        # None for that case — so mirror the probe's honest-None contract.
        try:
            me = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            me = None
        assert probes.username_of_uid(uid) == me
        assert probes.file_owner(tmp_path) == me

    # env snapshot probes: the snapshot is taken inside default_probes(), so
    # it must reflect the live process environment (PATH is a stable anchor).
    assert probes.env_value("PATH") == os.environ.get("PATH")
    assert isinstance(probes.ambient_env_names(), list)
    available_memory = probes.available_memory_bytes()
    assert isinstance(available_memory, int)
    assert available_memory > 0

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

    # PATH / install root probes: `modulo` is installed into this worktree's
    # venv, which `uv run` prepends to PATH, so the probe must resolve it.
    assert probes.install_root() is not None
    # The probe is `shutil.which("modulo")`; assert it agrees with the
    # environment-agnostic source rather than assuming the venv bin is on
    # PATH (a runner that does not prepend it would otherwise fail).
    assert probes.modulo_on_path() == shutil.which("modulo")

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

    # secrets mode reflects the real file (POSIX bit view; the documented
    # contract is None on Windows where the ACL seam is a TODO(P3)).
    (tmp_path / "secrets.json").chmod(0o600)
    secrets_mode_on_windows = sys.platform == "win32"
    if not secrets_mode_on_windows:
        assert probes.secrets_mode(tmp_path) == 0o600
    else:
        assert probes.secrets_mode(tmp_path) is None

    # bundled binaries / bundle version resolve (may be empty if no bundle)
    assert isinstance(probes.bundled_binaries(), list)
    # Only assume "no bundled postgres" when the bundle list itself is empty;
    # a bundle that does carry postgres is allowed to report a version (its
    # dotted-token parse path is owned by
    # test_probe_bundle_pg_version_parses_dotted_token in test_doctor.py).
    if not probes.bundled_binaries():
        assert probes.bundle_pg_version() is None

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


# ---------------------------------------------------------------------------
# Defensive / platform branches moved by the S3776 refactor
# ---------------------------------------------------------------------------


def test_probe_redis_ping_false_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeClient:
        def ping(self) -> bool:
            return False

    fake_redis = SimpleNamespace(Redis=lambda **_kwargs: _FakeClient())

    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    with pytest.raises(RuntimeError, match="redis PING failed"):
        doctor_module._probe_redis_ping(16379, "redis://:pw@127.0.0.1:16379/0")


def test_probe_username_of_uid_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert doctor_module._probe_username_of_uid(0) is None


def test_probe_username_of_uid_without_pwd_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "pwd", None)
    assert doctor_module._probe_username_of_uid(0) is None


def test_probe_listening_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not doctor_module._probe_listening_on(5432)


def test_probe_secrets_mode_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert doctor_module._probe_secrets_mode(tmp_path) is None


def test_probe_secrets_mode_missing_file(tmp_path: Path) -> None:
    assert doctor_module._probe_secrets_mode(tmp_path) is None


def test_probe_last_backup_at_without_state() -> None:
    assert doctor_module._probe_last_backup_at(None) is None


def test_probe_last_backup_at_without_timestamp() -> None:
    assert doctor_module._probe_last_backup_at(SimpleNamespace(last_backup_at=None)) is None


def test_probe_last_backup_at_with_unparseable_timestamp() -> None:
    state = SimpleNamespace(last_backup_at="not-a-timestamp")
    assert doctor_module._probe_last_backup_at(state) is None


def test_listen_hosts_in_table_missing_file(tmp_path: Path) -> None:
    assert not doctor_module._listen_hosts_in_table(tmp_path / "absent.tcp", socket.AF_INET, 5432)


def test_listen_hosts_in_table_undecodable_file(tmp_path: Path) -> None:
    table = tmp_path / "tcp"
    table.write_bytes(b"  sl  local_address rem_address   st\n\xff\xfe\n")
    assert not doctor_module._listen_hosts_in_table(table, socket.AF_INET, 5432)


def test_listen_hosts_in_table_skips_undecodable_address(tmp_path: Path) -> None:
    # Port 1F90 (8080) matches, so the malformed host hex reaches the decoder
    # and the per-line except continues instead of aborting the whole table.
    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st\n"
        "   0: ZZZZZZZZ:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000\n",
        encoding="ascii",
    )
    assert not doctor_module._listen_hosts_in_table(table, socket.AF_INET, 8080)


def test_port_owner_descriptions_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert not doctor_module._port_owner_descriptions(5432)
