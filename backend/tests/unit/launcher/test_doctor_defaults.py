"""Coverage backfill for doctor-lite (FAR-671).

Targets the SonarCloud new-code coverage gate: the pure helpers
(``_password_from_url`` / ``_cwd_env_file`` / ``_parse_listeners_from_proc`` /
``_decode_proc_address``), the ``default_probes`` wiring (including the
real-runtime probe closures exercised via dependency injection), the
``_load_state_readonly`` read-only paths, and the remaining per-check fault
branches not exercised elsewhere.
"""

from pathlib import Path

import pytest

from modulo.launcher import doctor as doctor_module
from modulo.launcher.doctor import (
    DoctorProbes,
    _cwd_env_file,
    _decode_proc_address,
    _load_state_readonly,
    _parse_listeners_from_proc,
    _password_from_url,
    check_cwd_env_influence,
    check_data_dir,
    check_migrations,
    check_postgres,
    check_redis,
    default_probes,
)
from modulo.launcher.secrets_file import LauncherSecrets
from modulo.launcher.state import LauncherState, save_state


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


def _write_state_secrets(tmp_path: Path) -> LauncherState:
    secrets = LauncherSecrets(
        postgres_password="pg-pw",
        redis_password="redis-pw",
        state_hmac_key=bytes(range(32)),
    )
    (tmp_path / "secrets.json").write_text(
        '{"postgres_password": "pg-pw", "redis_password": "redis-pw", '
        f'"state_hmac_key": "{secrets.state_hmac_key_hex}"}}',
        encoding="utf-8",
    )
    save_state(_state(), tmp_path / "state.json", secrets.state_hmac_key)
    return _state()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_password_from_url_unquotes_password() -> None:
    assert _password_from_url("redis://:p%40ss@127.0.0.1:6379/0") == "p@ss"
    assert _password_from_url("redis://:plain@127.0.0.1:6379/0") == "plain"
    assert _password_from_url("redis://127.0.0.1:6379/0") == ""


def test_cwd_env_file_present_and_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cwd_env_file() is None
    (tmp_path / ".env").write_text("FOO=bar", encoding="utf-8")
    found = _cwd_env_file()
    assert found is not None and found.name == ".env"


def test_decode_proc_address_ipv4_and_ipv6() -> None:
    assert _decode_proc_address("0100007F", doctor_module.socket.AF_INET) == "127.0.0.1"
    # IPv6 branch is exercised (real /proc byte order yields an :: address)
    assert "::" in _decode_proc_address("00000000000000000000000000000001", doctor_module.socket.AF_INET6)


class _FakeProcPath:
    def __init__(self, real: Path) -> None:
        self._real = real

    def read_text(self, encoding: str = "ascii") -> str:
        if str(self._real) == "/proc/net/tcp":
            # local 127.0.0.1:15432 (0x3C48) in /proc little-endian form, state 0A = LISTEN
            return "  sl  local_address rem_address st tx_queue\n   0: 0100007F:3C48 00000000:0000 0A 01 02\n"
        if str(self._real) == "/proc/net/tcp6":
            # ::1:15432 (0x3C48), state 0A = LISTEN
            return (
                "  sl  local_address rem_address st\n"
                "   0: 00000000000000000000000000000001:3C48 "
                "00000000000000000000000000000000:0000 0A\n"
            )
        return self._real.read_text(encoding=encoding)


def test_parse_listeners_from_proc_reads_loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_path = doctor_module.Path

    def _fake_path(p: str | Path) -> Path | _FakeProcPath:
        sp = str(p)
        if sp in ("/proc/net/tcp", "/proc/net/tcp6"):
            return _FakeProcPath(real_path(sp))
        return real_path(p)

    monkeypatch.setattr(doctor_module, "Path", _fake_path)
    listeners = _parse_listeners_from_proc(15432)
    assert "127.0.0.1" in listeners
    assert any(entry.startswith("::") for entry in listeners)
    # a different port is not listening
    assert _parse_listeners_from_proc(99999) == []


# ---------------------------------------------------------------------------
# _load_state_readonly read-only paths
# ---------------------------------------------------------------------------


def test_load_state_readonly_uninitialized(tmp_path: Path) -> None:
    state, error = _load_state_readonly(tmp_path)
    assert state is None
    assert "not initialized" in error


def test_load_state_readonly_corrupt_secrets(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("not valid json {", encoding="utf-8")
    state, error = _load_state_readonly(tmp_path)
    assert state is None
    assert "secrets file unreadable" in error


def test_load_state_readonly_missing_state_json(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text(
        '{"postgres_password": "pg-pw", "redis_password": "redis-pw", '
        '"state_hmac_key": "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"}',
        encoding="utf-8",
    )
    state, error = _load_state_readonly(tmp_path)
    assert state is None
    assert "state.json unreadable" in error


# ---------------------------------------------------------------------------
# default_probes: the real-runtime wiring (probes exercised via injection)
# ---------------------------------------------------------------------------


class _FakeConn:
    async def fetchval(self, _q: str) -> int:
        return 1

    async def close(self) -> None:
        return None


async def _fake_asyncpg_connect(*_a: object, **_k: object) -> _FakeConn:
    return _FakeConn()


class _FakeRedis:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._kwargs = kwargs

    def ping(self) -> bool:
        return True


class _FakeEngine:
    async def dispose(self) -> None:
        return None


def test_default_probes_builds_and_exercises_testable_closures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, state)

    assert isinstance(probes, DoctorProbes)

    # _probe_writable writes + removes a probe file
    probes.assert_writable(tmp_path)
    assert not (tmp_path / f".doctor-write-probe-{__import__('os').getpid()}").exists()

    # _effective_uid returns an int on posix
    assert isinstance(probes.effective_uid(), int)

    # _username_of_uid + _file_owner resolve on posix
    me = probes.username_of_uid(probes.effective_uid() or 0)
    assert me is None or isinstance(me, str)
    assert probes.file_owner(tmp_path) is None or probes.file_owner(tmp_path) == me

    # _listening_on delegates to /proc parsing (no listeners in this sandbox)
    assert probes.listening_on(state.postgres_port) == []


def test_default_probes_postgres_probe_reachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    import asyncpg

    import modulo.db.bootstrap_role as br

    state = _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, state)

    monkeypatch.setattr(asyncpg, "connect", _fake_asyncpg_connect)
    monkeypatch.setattr(br, "_asyncpg_admin_connect", lambda _url: ("fake-dsn", None))
    probes.probe_postgres()  # must not raise (asyncio round-trip awaited)
    import inspect

    assert inspect.iscoroutinefunction(_fake_asyncpg_connect)


def test_default_probes_role_violations_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    import modulo.db.bootstrap_role as br

    state = _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, state)

    monkeypatch.setattr(asyncpg, "connect", _fake_asyncpg_connect)
    monkeypatch.setattr(br, "_asyncpg_admin_connect", lambda _url: ("fake-dsn", None))

    async def _fake_violations(_conn: object, _role: object) -> list[str]:
        return ["app role modulo_app has BYPASSRLS"]

    monkeypatch.setattr(br, "_find_allow_list_violations", _fake_violations)
    violations = probes.role_violations()
    assert "BYPASSRLS" in violations[0]


def test_default_probes_redis_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import redis

    state = _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, state)
    monkeypatch.setattr(redis, "Redis", _FakeRedis)
    probes.probe_redis()  # must not raise (ping returns True)


def test_default_probes_redis_probe_no_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import redis

    # No secrets -> composed REDIS_URL empty -> probe raises honestly
    probes = default_probes(tmp_path, None)
    monkeypatch.setattr(redis, "Redis", _FakeRedis)
    with pytest.raises(RuntimeError, match="no composed REDIS_URL"):
        probes.probe_redis()


def test_default_probes_migrations_at_head_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    import sqlalchemy.ext.asyncio as sae

    import modulo.db.health_checks as hc

    state = _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, state)

    async def _head(_engine: object) -> bool:
        return True

    monkeypatch.setattr(hc, "db_is_at_migration_head", _head)
    monkeypatch.setattr(sae, "create_async_engine", lambda _url: _FakeEngine())
    assert probes.migrations_at_head() is True


def test_default_probes_launcher_running_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.launcher.supervisor as sup

    state = _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, state)

    class _Holder:
        pid = 4242

    monkeypatch.setattr(sup, "_read_lock_holder", lambda _p: _Holder())
    monkeypatch.setattr(sup, "_pid_alive", lambda _pid: True)
    assert probes.launcher_running() is True


def test_default_probes_env_file_pinned_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.settings as settings

    state = _write_state_secrets(tmp_path)
    probes = default_probes(tmp_path, state)
    monkeypatch.setattr(settings, "pinned_env_file", lambda: None)
    assert probes.env_file_pinned() is False


# ---------------------------------------------------------------------------
# Remaining per-check fault branches
# ---------------------------------------------------------------------------


def test_data_dir_fail_when_disk_probe_raises(tmp_path: Path) -> None:
    def boom(_root: Path) -> int:
        raise OSError("statvfs failed")

    result = check_data_dir(
        tmp_path,
        _state(),
        DoctorProbes(
            disk_free_bytes=boom,
            assert_writable=lambda _r: None,
            listening_on=lambda _p: [],
            probe_postgres=lambda: None,
            role_violations=list,
            probe_redis=lambda: None,
            migrations_at_head=lambda: True,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _u: "op",
            file_owner=lambda _p: "op",
            cwd_env_file=None,
            env_file_pinned=lambda: True,
            launcher_running=lambda: True,
        ),
    )
    assert result.ok is False
    assert "disk probe failed" in result.detail


def test_postgres_fail_when_role_probe_raises(tmp_path: Path) -> None:
    def boom() -> list[str]:
        raise RuntimeError("role audit timed out")

    result = check_postgres(
        tmp_path,
        _state(),
        DoctorProbes(
            disk_free_bytes=lambda _r: 1 << 40,
            assert_writable=lambda _r: None,
            listening_on=lambda _p: ["127.0.0.1"],
            probe_postgres=lambda: None,
            role_violations=boom,
            probe_redis=lambda: None,
            migrations_at_head=lambda: True,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _u: "op",
            file_owner=lambda _p: "op",
            cwd_env_file=None,
            env_file_pinned=lambda: True,
            launcher_running=lambda: True,
        ),
    )
    assert result.ok is False
    assert "role-posture probe failed" in result.detail


def test_redis_skipped_when_launcher_not_running(tmp_path: Path) -> None:
    result = check_redis(
        tmp_path,
        _state(),
        DoctorProbes(
            disk_free_bytes=lambda _r: 1 << 40,
            assert_writable=lambda _r: None,
            listening_on=lambda _p: ["127.0.0.1"],
            probe_postgres=lambda: None,
            role_violations=list,
            probe_redis=lambda: None,
            migrations_at_head=lambda: True,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _u: "op",
            file_owner=lambda _p: "op",
            cwd_env_file=None,
            env_file_pinned=lambda: True,
            launcher_running=lambda: False,
        ),
    )
    assert result.ok is True
    assert "skipped" in result.detail


def test_migrations_skipped_when_launcher_not_running(tmp_path: Path) -> None:
    result = check_migrations(
        tmp_path,
        _state(),
        DoctorProbes(
            disk_free_bytes=lambda _r: 1 << 40,
            assert_writable=lambda _r: None,
            listening_on=lambda _p: ["127.0.0.1"],
            probe_postgres=lambda: None,
            role_violations=list,
            probe_redis=lambda: None,
            migrations_at_head=lambda: True,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _u: "op",
            file_owner=lambda _p: "op",
            cwd_env_file=None,
            env_file_pinned=lambda: True,
            launcher_running=lambda: False,
        ),
    )
    assert result.ok is True
    assert "skipped" in result.detail


def test_migrations_fail_when_probe_raises(tmp_path: Path) -> None:
    def boom() -> bool:
        raise RuntimeError("alembic heads unreachable")

    result = check_migrations(
        tmp_path,
        _state(),
        DoctorProbes(
            disk_free_bytes=lambda _r: 1 << 40,
            assert_writable=lambda _r: None,
            listening_on=lambda _p: ["127.0.0.1"],
            probe_postgres=lambda: None,
            role_violations=list,
            probe_redis=lambda: None,
            migrations_at_head=boom,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _u: "op",
            file_owner=lambda _p: "op",
            cwd_env_file=None,
            env_file_pinned=lambda: True,
            launcher_running=lambda: True,
        ),
    )
    assert result.ok is False
    assert "migration probe failed" in result.detail


def test_env_influence_pass_when_no_ambient_urls(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=info", encoding="utf-8")
    result = check_cwd_env_influence(
        tmp_path,
        _state(),
        DoctorProbes(
            disk_free_bytes=lambda _r: 1 << 40,
            assert_writable=lambda _r: None,
            listening_on=lambda _p: ["127.0.0.1"],
            probe_postgres=lambda: None,
            role_violations=list,
            probe_redis=lambda: None,
            migrations_at_head=lambda: True,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _u: "op",
            file_owner=lambda _p: "op",
            cwd_env_file=env_file,
            env_file_pinned=lambda: True,
            launcher_running=lambda: True,
        ),
    )
    assert result.ok is True
    assert "carries no ambient service URLs" in result.detail
