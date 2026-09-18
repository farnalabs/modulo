"""Coverage tests for doctor-lite helpers + default_probes wiring (FAR-671).

The existing test_doctor.py covers the pure check functions and the
run_doctor orchestration via injected probes. This module pins down the
real-launcher wiring that those tests deliberately leave to the probe
interface: the pure helpers (_password_from_url, _cwd_env_file,
_parse_listeners_from_proc, _decode_proc_address), the read-only state
loader's error branches, and default_probes (every injected probe body,
including the service probes which are exercised against mocked
asyncpg/redis/engine so the connection paths are covered without a live
database).
"""

import json
import socket
import struct
import sys
from pathlib import Path

import pytest

import modulo.db.bootstrap_role as bootstrap_role
import modulo.db.health_checks as health_checks
from modulo.launcher.doctor import (
    DoctorProbes,
    _cwd_env_file,
    _decode_proc_address,
    _host_port_from_database_url,
    _load_state_readonly,
    _parse_listeners_from_proc,
    _password_from_url,
    _state_problem_kind,
    check_cwd_env_influence,
    check_migrations,
    check_postgres,
    check_redis,
    default_probes,
)
from modulo.launcher.secrets_file import LauncherSecrets
from modulo.launcher.state import LauncherState, save_state


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


def _write_secrets(tmp_path: Path) -> LauncherSecrets:
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
    return secrets


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_password_from_url_with_and_without_password() -> None:
    assert _password_from_url("redis://:topsecret@127.0.0.1:6379") == "topsecret"
    assert not _password_from_url("redis://127.0.0.1:6379")
    assert _password_from_url("redis://user:with%23hash@127.0.0.1:6379") == "with#hash"


def test_cwd_env_file_absent_and_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cwd_env_file() is None
    env_file = tmp_path / ".env"
    env_file.write_text("X=1", encoding="utf-8")
    assert _cwd_env_file() == env_file


def test_decode_proc_address_ipv4_and_ipv6() -> None:
    assert _decode_proc_address("0100007F", socket.AF_INET) == "127.0.0.1"
    expected_ipv6 = socket.inet_ntop(socket.AF_INET6, struct.pack("<4I", 0, 0, 0, 1))
    assert _decode_proc_address("00000000000000000000000000000001", socket.AF_INET6) == expected_ipv6


def test_parse_listeners_from_proc_closed_port_is_empty() -> None:
    # port 1 is never listening in a healthy sandbox
    assert not _parse_listeners_from_proc(1)


@pytest.mark.skipif(sys.platform == "win32", reason="/proc listener inspection is POSIX-only (TODO(P3))")
def test_parse_listeners_from_proc_detects_loopback_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        hosts = _parse_listeners_from_proc(port)
        assert "127.0.0.1" in hosts


# ---------------------------------------------------------------------------
# _load_state_readonly error branches
# ---------------------------------------------------------------------------


def test_load_state_readonly_no_secrets(tmp_path: Path) -> None:
    state, err = _load_state_readonly(tmp_path)
    assert state is None
    assert err is not None
    assert "not initialized" in err


def test_load_state_readonly_unreadable_secrets(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{not valid json", encoding="utf-8")
    state, err = _load_state_readonly(tmp_path)
    assert state is None
    assert err is not None
    assert "unreadable" in err


def test_load_state_readonly_no_state_file(tmp_path: Path) -> None:
    _write_secrets(tmp_path)
    state, err = _load_state_readonly(tmp_path)
    assert state is None
    assert err is not None
    assert "state.json" in err
    assert "unreadable" in err


def test_load_state_readonly_integrity_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_secrets(tmp_path)
    save_state(_state(), tmp_path / "state.json", bytes(range(32)))

    def _boom(_path: Path, _key: bytes) -> LauncherState:
        from modulo.launcher.state import StateIntegrityError

        raise StateIntegrityError("tampered")

    import modulo.launcher.state as state_module

    monkeypatch.setattr(state_module, "load_state", _boom)
    state, err = _load_state_readonly(tmp_path)
    assert state is None
    assert err is not None
    assert "state.json unreadable" in err


# ---------------------------------------------------------------------------
# default_probes — every injected probe body, no live services
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="effective_uid + /proc-backed probes are POSIX-only (TODO(P3))")
def test_default_probes_state_none_exercises_all_probes(tmp_path: Path) -> None:
    """With no state, composed is empty so the service probes raise their
    honest 'no composed URL' errors and the light probes run for real."""
    probes = default_probes(tmp_path, None)
    assert isinstance(probes.disk_free_bytes(tmp_path), int)
    probes.assert_writable(tmp_path)  # must not raise
    uid = probes.effective_uid()
    assert uid is not None
    assert probes.username_of_uid(uid) is not None
    assert probes.file_owner(tmp_path) is not None
    assert not probes.listening_on(1)  # no listener on port 1
    assert probes.launcher_running() is False
    assert probes.env_file_pinned() is False
    assert probes.cwd_env_file is None

    for probe in (probes.probe_postgres, probes.role_violations, probes.probe_redis, probes.migrations_at_head):
        with pytest.raises(RuntimeError, match="no composed"):
            probe()


class _FakeConn:
    async def fetchval(self, _q: str) -> int:
        return 1

    async def close(self) -> None:
        pass


class _FakeAsyncpg:
    @staticmethod
    async def connect(_dsn: str, **_kwargs: object) -> _FakeConn:
        return _FakeConn()


class _FakeRedisClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def ping(self) -> bool:
        return True


class _FakeRedis:
    Redis = _FakeRedisClient


class _FakeEngine:
    async def dispose(self) -> None:
        pass


def test_default_probes_service_probes_connect_with_mocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the postgres/redis/migrations connection paths behind mocks so
    the async connect bodies are covered without a live database."""
    secrets = _write_secrets(tmp_path)
    save_state(_state(), tmp_path / "state.json", secrets.state_hmac_key)
    from modulo.launcher.state import load_state as _load

    loaded_state = _load(tmp_path / "state.json", secrets.state_hmac_key)

    monkeypatch.setitem(sys.modules, "asyncpg", _FakeAsyncpg)
    monkeypatch.setattr(bootstrap_role, "_asyncpg_admin_connect", lambda _url: ("dsn", None))

    async def _fake_violations(_c: object, _r: object) -> list[str]:
        return []

    monkeypatch.setattr(bootstrap_role, "_find_allow_list_violations", _fake_violations)
    monkeypatch.setattr(bootstrap_role, "_parse_role", lambda _u: "modulo_app")
    monkeypatch.setitem(sys.modules, "redis", _FakeRedis)

    async def _fake_at_head(_e: object) -> bool:
        return True

    monkeypatch.setattr(health_checks, "db_is_at_migration_head", _fake_at_head)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", lambda _url: _FakeEngine())

    probes = default_probes(tmp_path, loaded_state)
    probes.probe_postgres()  # connect -> SELECT 1
    assert not probes.role_violations()  # connect -> audit -> []
    probes.probe_redis()  # ping -> True
    assert probes.migrations_at_head() is True  # engine -> at head


# ---------------------------------------------------------------------------
# FAR-676 review fixes: wired probes + honest-skip helpers
# ---------------------------------------------------------------------------


def test_host_port_from_database_url_out_of_range_port_is_untyped_skip() -> None:
    """A malformed ambient DATABASE_URL (port 99999) must degrade to an honest
    ('unknown', None) skip, not crash the settings-source check (exit 1)."""
    host, port = _host_port_from_database_url("postgres://user:pass@127.0.0.1:99999/app")
    assert host == "unknown"
    assert port is None


@pytest.mark.skipif(sys.platform == "win32", reason="/proc listener inspection is POSIX-only (TODO(P3))")
def test_default_probes_port_owner_no_foreign_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.launcher.doctor as doctor_module

    monkeypatch.setattr(doctor_module, "_port_owner_descriptions", lambda _port: [])
    probes = default_probes(tmp_path, _state())
    assert probes.port_owner_description(15432) is None


@pytest.mark.skipif(sys.platform == "win32", reason="/proc listener inspection is POSIX-only (TODO(P3))")
def test_default_probes_port_owner_describes_foreign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.launcher.doctor as doctor_module

    monkeypatch.setattr(
        doctor_module,
        "_port_owner_descriptions",
        lambda _port: ["dockerd (pid 4242) bound to 0.0.0.0"],
    )
    probes = default_probes(tmp_path, _state())
    assert probes.port_owner_description(15432) == "dockerd (pid 4242) bound to 0.0.0.0"


def test_default_probes_last_backup_at_wired(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    state = LauncherState(
        postgres_port=15432,
        redis_port=16379,
        api_port=18000,
        last_backup_at=datetime.now(UTC).isoformat(),
    )
    probes = default_probes(tmp_path, state)
    stamp = probes.last_backup_at()
    assert isinstance(stamp, float)
    # within the last minute of now
    assert abs(stamp - datetime.now(UTC).timestamp()) < 60


def test_default_probes_last_backup_at_none_when_unset(tmp_path: Path) -> None:
    probes = default_probes(tmp_path, _state())
    assert probes.last_backup_at() is None


def test_default_probes_tls_expiry_none_without_tls_dir(tmp_path: Path) -> None:
    probes = default_probes(tmp_path, _state())
    assert probes.tls_expiry() is None


def test_state_problem_kind_missing_when_secrets_present_but_state_absent(tmp_path: Path) -> None:
    """secrets.json present + no state.json must classify as 'missing'
    (uninitialized), never the misleading 'corrupt' (torn write) language."""
    _write_secrets(tmp_path)
    _, state_error = _load_state_readonly(tmp_path)
    assert state_error is not None
    assert _state_problem_kind(tmp_path, state_error) == "missing"


# ---------------------------------------------------------------------------
# missing check branches the injected-probe suite didn't hit
# ---------------------------------------------------------------------------


def _probes_with(**overrides: object) -> DoctorProbes:
    probes = DoctorProbes(
        disk_free_bytes=lambda _r: 40 * 1024 * 1024 * 1024,
        assert_writable=lambda _r: None,
        listening_on=lambda _p: ["127.0.0.1"],
        probe_postgres=lambda: None,
        role_violations=list,
        probe_redis=lambda: None,
        migrations_at_head=lambda: True,
        effective_uid=lambda: 1000,
        username_of_uid=lambda _u: "op",
        file_owner=lambda _p: "op",
        launcher_running=lambda: True,
    )
    for key, value in overrides.items():
        setattr(probes, key, value)
    return probes


def test_postgres_role_posture_probe_raising_becomes_failed_check(tmp_path: Path) -> None:
    def boom() -> list[str]:
        raise RuntimeError("audit driver down")

    result = check_postgres(tmp_path, _state(), _probes_with(role_violations=boom))
    assert result.ok is False
    assert "role-posture probe failed" in result.detail


def test_redis_state_none_reports_unreadable(tmp_path: Path) -> None:
    result = check_redis(tmp_path, None, default_probes(tmp_path, None))
    assert result.ok is False
    assert "state.json" in result.detail


def test_migrations_state_none_reports_unreadable(tmp_path: Path) -> None:
    result = check_migrations(tmp_path, None, default_probes(tmp_path, None))
    assert result.ok is False
    assert "state.json" in result.detail


def test_migrations_probe_raising_becomes_failed_check(tmp_path: Path) -> None:
    def boom() -> bool:
        raise RuntimeError("migration audit down")

    from modulo.launcher.doctor import DoctorProbes

    probes = DoctorProbes(
        disk_free_bytes=lambda _r: 1,
        assert_writable=lambda _r: None,
        listening_on=lambda _p: [],
        probe_postgres=lambda: None,
        role_violations=list,
        probe_redis=lambda: None,
        migrations_at_head=boom,
        effective_uid=lambda: 1000,
        username_of_uid=lambda _u: "op",
        file_owner=lambda _p: "op",
        launcher_running=lambda: True,
    )
    result = check_migrations(tmp_path, _state(), probes)
    assert result.ok is False
    assert "migration probe failed" in result.detail


def test_env_influence_present_but_no_ambient_urls_is_ok(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PLAIN_VAR=no_urls_here\n", encoding="utf-8")
    result = check_cwd_env_influence(tmp_path, _state(), _probes_with(cwd_env_file=env_file))
    assert result.ok is True
    assert "no ambient service URLs" in result.detail


def test_redis_skipped_when_launcher_not_running(tmp_path: Path) -> None:
    result = check_redis(tmp_path, _state(), _probes_with(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_migrations_skipped_when_launcher_not_running(tmp_path: Path) -> None:
    result = check_migrations(tmp_path, _state(), _probes_with(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_default_probes_username_of_uid_unknown_uid_is_none(tmp_path: Path) -> None:
    probes = default_probes(tmp_path, None)
    # uid 999999 is not a real account -> KeyError path returns None
    assert probes.username_of_uid(999999) is None


def test_default_probes_file_owner_missing_path_is_none(tmp_path: Path) -> None:
    probes = default_probes(tmp_path, None)
    assert probes.file_owner(tmp_path / "does-not-exist") is None
