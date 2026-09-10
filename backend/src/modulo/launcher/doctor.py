"""Doctor-lite: ``modulo doctor`` (FAR-671 slice 3, final slice).

Five core checks plus two safety checks, each a PURE function over an
INJECTED probe interface (:class:`DoctorProbes`) so every pass/fail path is
unit-testable without services. Probe exceptions are converted into failed
checks with the detail attached — the doctor never exits through an
unhandled exception.

Checks (exit-code convention: 0 = healthy, 1 = unhealthy):

1. ``data-dir`` — writable + quantified free-disk
   floor (:data:`MIN_FREE_DISK_BYTES`).
2. ``ports`` — loopback bind assertions for the configured PG/Redis/API
   ports from state.json, actual listeners vs configured (only when the
   launcher is running; skipped honestly otherwise).
3. ``postgres`` — reachable + bootstrap-role posture via the promoted
   ``modulo.db.bootstrap_role`` predicate (``modulo_app`` NOBYPASSRLS, the
   app role never a superuser, ``modulo_system`` BYPASSRLS, ...) so doctor
   and boot can never disagree about healthy.
4. ``redis`` — ping + auth on loopback.
5. ``migrations`` — current at the alembic head (the same promoted
   ``modulo.db.health_checks.db_is_at_migration_head`` predicate the boot
   fast-path uses).
6. ``env-influence`` — a CWD ``.env`` must not be able to steer Settings
   (refused when ambient URLs sit in an unpinned CWD ``.env``).
7. ``privileges`` — root/sudo refusal + data-dir ownership mismatch.

Every predicate reused from the boot path (“doctor and boot must never
disagree”): ``bootstrap_role._find_allow_list_violations``,
``health_checks.db_is_at_migration_head``, ``env_safety``, the supervisor
lock/holder readers. TODO(P3): Windows privilege/ownership probes (uid/pwd
are POSIX) — reported as an honest skip, never a silent pass.
"""

import asyncio
import os
import shutil
import socket
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Quantified disk floor for a healthy single-install data dir.
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024

MB = 1024 * 1024
GB = 1024 * MB

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

__all__ = [
    "MIN_FREE_DISK_BYTES",
    "CheckResult",
    "DoctorProbes",
    "default_probes",
    "run_doctor",
]


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome; ``detail`` carries the actionable text."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class DoctorProbes:
    """Injected probe interface — every check is pure over these callables.

    Contract per probe: raise (or return the failure form documented
    inline) to make its check FAIL; the doctor converts any probe exception
    into a failed check with the exception text as the detail.
    """

    # int free bytes on the filesystem holding the data dir.
    disk_free_bytes: Callable[[Path], int]
    # raises when the root is not writable (RuntimeError/OSError).
    assert_writable: Callable[[Path], None]
    # hosts currently bound to this port (LISTEN sockets); empty = unknown.
    listening_on: Callable[[int], list[str]]
    # DB admin probe: raises when the bundled Postgres is not reachable.
    probe_postgres: Callable[[], None]
    # role posture violations (reuse of bootstrap_role's audit predicate).
    role_violations: Callable[[], list[str]]
    # redis probe: raises when ping/auth fails on the configured port.
    probe_redis: Callable[[], None]
    # True when the DB's alembic_version equals the head; raises -> failed.
    migrations_at_head: Callable[[], bool]
    # POSIX uid or None on platforms without one (TODO(P3) Windows).
    effective_uid: Callable[[], int | None]
    # username for a uid, or None (unknown / platform seam).
    username_of_uid: Callable[[int], str | None]
    # owner username of *path*, or None when unknown.
    file_owner: Callable[[Path], str | None]
    # the CWD .env path when it exists, else None.
    cwd_env_file: Path | None = field(default=None)
    # True when Settings' env file is pinned (launcher-pinned config).
    env_file_pinned: Callable[[], bool] = field(default=lambda: False)
    # True when the per-data-dir launcher process is alive.
    launcher_running: Callable[[], bool] = field(default=lambda: False)


# ---------------------------------------------------------------------------
# Checks (pure functions over the probes)
# ---------------------------------------------------------------------------


def check_data_dir(data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 1 — writable data dir + quantified disk floor."""
    try:
        free = probes.disk_free_bytes(data_dir)
    except Exception as exc:
        return CheckResult("data-dir", False, f"disk probe failed: {exc}")
    if free < MIN_FREE_DISK_BYTES:
        return CheckResult(
            "data-dir",
            False,
            f"only {free / MB:.0f} MiB free below {data_dir!s} (floor {MIN_FREE_DISK_BYTES // MB} MiB) — "
            "free space before upgrading or rebuilding the bundled Postgres",
        )
    try:
        probes.assert_writable(data_dir)
    except Exception as exc:
        return CheckResult("data-dir", False, f"data dir not writable: {exc}")
    return CheckResult(
        "data-dir",
        True,
        f"writable, {free / GB:.1f} GiB free of the {MIN_FREE_DISK_BYTES // MB} MiB floor",
    )


def check_ports(data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 2 — configured PG/Redis/API ports vs actual loopback listeners."""
    if state is None:
        return CheckResult("ports", False, "state.json unreadable — cannot compare configured vs actual ports")
    if not probes.launcher_running():
        return CheckResult("ports", True, "launcher not running — live port assertions skipped")
    listeners_postgres = probes.listening_on(state.postgres_port)
    listeners_redis = probes.listening_on(state.redis_port)
    listeners_api = probes.listening_on(state.api_port)
    problems: list[str] = []
    for name, port, listeners in (
        ("postgres", state.postgres_port, listeners_postgres),
        ("redis", state.redis_port, listeners_redis),
        ("api", state.api_port, listeners_api),
    ):
        if not listeners:
            problems.append(f"{name} is NOT listening on configured port {port}")
            continue
        foreign = sorted(h for h in listeners if h.lower() not in LOOPBACK_HOSTS)
        if foreign:
            problems.append(f"{name} port {port} is bound outside loopback on {', '.join(foreign)}")
    if problems:
        return CheckResult("ports", False, "; ".join(problems))
    return CheckResult(
        "ports",
        True,
        f"postgres/redis/api listening on loopback ports "
        f"{state.postgres_port}/{state.redis_port}/{state.api_port} as configured",
    )


def check_postgres(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 3 — bundled Postgres reachable + role posture (boot predicates)."""
    if state is None:
        return CheckResult("postgres", False, "state.json unreadable — cannot verify postgres port/posture")
    if not probes.launcher_running():
        return CheckResult("postgres", True, "launcher not running — postgres posture check skipped")
    try:
        probes.probe_postgres()
    except Exception as exc:
        return CheckResult(
            "postgres",
            False,
            f"not reachable on 127.0.0.1:{state.postgres_port}: {exc} — is the launcher running?",
        )
    try:
        violations = probes.role_violations()
    except Exception as exc:
        return CheckResult("postgres", False, f"role-posture probe failed: {exc}")
    if violations:
        return CheckResult(
            "postgres",
            False,
            f"reachable on 127.0.0.1:{state.postgres_port} but role posture DRIFTED: " + "; ".join(violations),
        )
    return CheckResult(
        "postgres",
        True,
        f"reachable on 127.0.0.1:{state.postgres_port}; role posture holds "
        "(modulo_app NOBYPASSRLS/superuser=false, modulo_system BYPASSRLS)",
    )


def check_redis(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 4 — redis ping + auth on the configured loopback port."""
    if state is None:
        return CheckResult("redis", False, "state.json unreadable — cannot verify the configured redis port")
    if not probes.launcher_running():
        return CheckResult("redis", True, "launcher not running — redis ping skipped")
    try:
        probes.probe_redis()
    except Exception as exc:
        return CheckResult(
            "redis",
            False,
            f"redis ping failed on 127.0.0.1:{state.redis_port} (service down, wrong port or bad auth): {exc}",
        )
    return CheckResult("redis", True, f"answers PING with auth on 127.0.0.1:{state.redis_port}")


def check_migrations(_data_dir: Path, state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 5 — database at the alembic migration head (boot fast-path predicate)."""
    if state is None:
        return CheckResult("migrations", False, "state.json unreadable — cannot locate the bundled database")
    if not probes.launcher_running():
        return CheckResult("migrations", True, "launcher not running — migration check skipped")
    try:
        at_head = probes.migrations_at_head()
    except Exception as exc:
        return CheckResult("migrations", False, f"migration probe failed: {exc}")
    if not at_head:
        return CheckResult(
            "migrations",
            False,
            "database is NOT at the alembic head — restart `modulo start` so the lifespan migration path runs",
        )
    return CheckResult("migrations", True, "database at the alembic migration head")


def check_cwd_env_influence(_data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 6 — a CWD ``.env`` must not be able to steer Settings."""
    env_file = probes.cwd_env_file
    if env_file is None:
        return CheckResult("env-influence", True, "no .env in the launch directory")
    from modulo.launcher.env_safety import _ambient_urls_from_env_file

    ambient = sorted(_ambient_urls_from_env_file(env_file))
    if not ambient:
        return CheckResult("env-influence", True, f"{env_file} present but carries no ambient service URLs")
    if probes.env_file_pinned():
        return CheckResult(
            "env-influence",
            True,
            f"{env_file} carries {', '.join(ambient)} but the launcher pinned its config — "
            "the CWD .env cannot influence Settings",
        )
    return CheckResult(
        "env-influence",
        False,
        f"{', '.join(ambient)} in {env_file} would influence Settings (no pinned "
        "launcher config) — rename/remove the file, or start with `modulo start`",
    )


def check_privileges(data_dir: Path, _state: Any, probes: DoctorProbes) -> CheckResult:
    """Check 7 — root/sudo refusal + data-dir ownership mismatch."""
    uid = probes.effective_uid()
    if uid is None:
        return CheckResult("privileges", True, "uid unavailable on this platform (TODO(P3) Windows equivalents)")
    if uid == 0:
        return CheckResult(
            "privileges",
            False,
            "running as root/sudo — the bundled postgres refuses a root-owned data dir; "
            "start `modulo start` as an unprivileged user",
        )
    me = probes.username_of_uid(uid)
    owner = probes.file_owner(data_dir)
    if me is not None and owner is not None and owner != me:
        return CheckResult(
            "privileges",
            False,
            f"data dir {data_dir} is owned by '{owner}' but the launcher runs as '{me}' — "
            f"chown -R {me} {data_dir} (an ownership flip bricks the bundled Postgres on restart)",
        )
    who = f"'{me}'" if me is not None else f"uid {uid}"
    return CheckResult("privileges", True, f"running unprivileged as {who}; data-dir ownership consistent")


_CHECKS: tuple[Callable[[Path, Any, DoctorProbes], CheckResult], ...] = (
    check_data_dir,
    check_ports,
    check_postgres,
    check_redis,
    check_migrations,
    check_cwd_env_influence,
    check_privileges,
)

# crash-time check names must match the names the checks themselves emit.
_CRASH_NAMES: dict[str, str] = {
    "check_data_dir": "data-dir",
    "check_ports": "ports",
    "check_postgres": "postgres",
    "check_redis": "redis",
    "check_migrations": "migrations",
    "check_cwd_env_influence": "env-influence",
    "check_privileges": "privileges",
}


# ---------------------------------------------------------------------------
# Default probes (the real launcher runtime wiring)
# ---------------------------------------------------------------------------


def default_probes(data_dir: Path, state: Any) -> DoctorProbes:
    """Build the probe set used by ``run_doctor`` outside unit tests.

    READ-ONLY like ``collect_status``: the secrets file is parsed without the
    create-if-missing behaviour; a doctor run must never create or rewrite
    data-dir artefacts.
    """
    from modulo.launcher import secrets_file as secrets_file_module
    from modulo.launcher.config_source import compose_config
    from modulo.launcher.secrets_file import SecretsFileError

    composed: dict[str, str] = {}
    if state is not None:
        try:
            secrets = secrets_file_module._parse((data_dir / "secrets.json").read_bytes())
            composed = compose_config(state, secrets)
        except (SecretsFileError, OSError):
            composed = {}

    def _probe_writable(root: Path) -> None:
        probe_path = root / f".doctor-write-probe-{os.getpid()}"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink()

    def _effective_uid() -> int | None:
        return os.getuid() if hasattr(os, "getuid") else None

    def _username_of_uid(uid: int) -> str | None:
        if os.name != "posix":
            return None  # TODO(P3): Windows SID → account mapping
        try:
            import pwd
        except ImportError:
            return None
        try:
            record: Any = pwd.getpwuid(uid)
            name = getattr(record, "pw_name", None)
            return str(name) if name else None
        except KeyError:
            return None

    def _file_owner(path: Path) -> str | None:
        if not path.exists():
            return None
        return _username_of_uid(path.stat().st_uid)

    def _listening_on(port: int) -> list[str]:
        if sys.platform != "linux":
            return []  # /proc absent; TODO(P3) Windows/macOS external-bind inspection
        return _parse_listeners_from_proc(port)

    def _probe_postgres() -> None:
        admin_url = composed.get("DATABASE_ADMIN_URL") or ""
        if not admin_url:
            raise RuntimeError("no composed DATABASE_ADMIN_URL (state/secrets unavailable)")
        import asyncpg  # asyncpg does not publish a py.typed marker

        from modulo.db.bootstrap_role import _asyncpg_admin_connect

        dsn, ssl_arg = _asyncpg_admin_connect(admin_url)

        async def _go() -> None:
            conn = await asyncpg.connect(dsn, ssl=ssl_arg, timeout=5)
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

        asyncio.run(_go())

    def _probe_role_violations() -> list[str]:
        admin_url = composed.get("DATABASE_ADMIN_URL") or ""
        app_url = composed.get("DATABASE_URL") or ""
        if not admin_url or not app_url:
            raise RuntimeError("no composed database URLs (state/secrets unavailable)")
        import asyncpg

        from modulo.db.bootstrap_role import (
            _asyncpg_admin_connect,
            _find_allow_list_violations,
            _parse_role,
        )

        async def _go() -> list[str]:
            dsn, ssl_arg = _asyncpg_admin_connect(admin_url)
            conn = await asyncpg.connect(dsn, ssl=ssl_arg, timeout=5)
            try:
                return list(await _find_allow_list_violations(conn, _parse_role(app_url)))
            finally:
                await conn.close()

        return asyncio.run(_go())

    def _probe_redis() -> None:
        redis_url = composed.get("REDIS_URL") or ""
        if not redis_url:
            raise RuntimeError("no composed REDIS_URL (state/secrets unavailable)")
        import redis

        client = redis.Redis(
            host="127.0.0.1",
            port=state.redis_port,
            password=_password_from_url(redis_url),
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        if not client.ping():
            raise RuntimeError("redis PING failed")

    def _probe_migrations_at_head() -> bool:
        database_url = composed.get("DATABASE_URL") or ""
        if not database_url:
            raise RuntimeError("no composed DATABASE_URL (state/secrets unavailable)")

        from modulo.db.health_checks import db_is_at_migration_head

        async def _go() -> bool:
            from sqlalchemy.ext.asyncio import create_async_engine

            engine = create_async_engine(database_url)
            try:
                return bool(await db_is_at_migration_head(engine))
            finally:
                await engine.dispose()

        return asyncio.run(_go())

    def _probe_launcher_running() -> bool:
        from modulo.launcher.supervisor import LOCK_SUFFIX, _pid_alive, _read_lock_holder

        # Mirrors collect_status: the lock sibling of the data dir names the holder.
        lock_path = data_dir.parent / (data_dir.name + LOCK_SUFFIX)
        holder = _read_lock_holder(lock_path)
        return holder is not None and _pid_alive(holder.pid)

    def _probe_env_file_pinned() -> bool:
        from modulo.settings import pinned_env_file

        return pinned_env_file() is not None

    return DoctorProbes(
        disk_free_bytes=lambda root: shutil.disk_usage(str(root)).free,
        assert_writable=_probe_writable,
        listening_on=_listening_on,
        probe_postgres=_probe_postgres,
        role_violations=_probe_role_violations,
        probe_redis=_probe_redis,
        migrations_at_head=_probe_migrations_at_head,
        effective_uid=_effective_uid,
        username_of_uid=_username_of_uid,
        file_owner=_file_owner,
        cwd_env_file=_cwd_env_file(),
        env_file_pinned=_probe_env_file_pinned,
        launcher_running=_probe_launcher_running,
    )


def _password_from_url(redis_url: str) -> str:
    from urllib.parse import unquote, urlparse

    return unquote(urlparse(redis_url).password or "")


def _cwd_env_file() -> Path | None:
    candidate = Path.cwd() / ".env"
    return candidate if candidate.exists() else None


def _parse_listeners_from_proc(port: int) -> list[str]:
    """LISTEN sockets on *port* from /proc/net{,6}/tcp (Linux only)."""

    hosts: set[str] = set()
    for proc_path, family in ((Path("/proc/net/tcp"), socket.AF_INET), (Path("/proc/net/tcp6"), socket.AF_INET6)):
        try:
            lines = proc_path.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, ValueError):
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A = LISTEN
                continue
            host_hex, port_hex = fields[1].split(":")
            if int(port_hex, 16) != port:
                continue
            try:
                hosts.add(_decode_proc_address(host_hex, family))
            except (ValueError, OSError):
                continue
    return sorted(hosts)


def _decode_proc_address(host_hex: str, family: int) -> str:
    if family == socket.AF_INET:
        raw = struct.pack("<I", int(host_hex, 16))
        return socket.inet_ntop(socket.AF_INET, raw)
    words = [int(host_hex[i : i + 8], 16) for i in range(0, 32, 8)]
    raw = struct.pack("<4I", *words)
    return socket.inet_ntop(socket.AF_INET6, raw)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _load_state_readonly(data_dir: Path) -> tuple[Any, str | None]:
    """(state, error) — read-only; a missing/unverifiable state is an error, not a crash."""
    from modulo.launcher.secrets_file import SecretsFileError, _parse
    from modulo.launcher.state import STATE_FILENAME, StateIntegrityError, StateVersionError, load_state

    secrets_path = data_dir / "secrets.json"
    if not secrets_path.exists():
        return None, "data dir is not initialized (no secrets file) — run `modulo start` first"
    try:
        secrets = _parse(secrets_path.read_bytes())
    except (SecretsFileError, OSError) as exc:
        return None, f"secrets file unreadable: {exc}"
    try:
        return load_state(data_dir / STATE_FILENAME, secrets.state_hmac_key), None
    except (StateIntegrityError, StateVersionError) as exc:
        return None, f"state.json unreadable: {exc}"
    except FileNotFoundError:
        return None, f"no {STATE_FILENAME} in {data_dir} — run `modulo start` first"


def run_doctor(data_dir: Path, *, as_json: bool = False, probes: DoctorProbes | None = None) -> int:
    """Run every check, emit the report (human table or --json), return 0/1."""
    state, state_error = _load_state_readonly(data_dir)
    built = probes if probes is not None else default_probes(data_dir, state)
    results: list[CheckResult] = []
    if state_error:
        info = f"state unavailable: {state_error}"
        results.extend(CheckResult(name, False, info) for name in ("ports", "postgres", "redis", "migrations"))
    for check in _CHECKS:
        try:
            results.append(check(data_dir, state, built))
        except Exception as exc:
            name = _CRASH_NAMES.get(check.__name__, check.__name__.removeprefix("check_"))
            results.append(CheckResult(name, False, f"check crashed: {exc}"))
    healthy = all(result.ok for result in results)
    if as_json:
        import json

        print(json.dumps(_payload_json(data_dir, healthy, results), indent=2, sort_keys=True))  # noqa: T201 — CLI output
    else:
        _print_report(data_dir, results, healthy)
    return 0 if healthy else 1


def _payload_json(data_dir: Path, healthy: bool, results: list[CheckResult]) -> dict[str, Any]:
    return {
        "data_dir": str(data_dir),
        "healthy": healthy,
        "checks": [{"name": result.name, "ok": result.ok, "detail": result.detail} for result in results],
    }


def _print_report(data_dir: Path, results: list[CheckResult], healthy: bool) -> None:
    print(f"modulo doctor — data dir: {data_dir}")  # noqa: T201
    width = max(len(result.name) for result in results)
    for result in results:
        status = "ok  " if result.ok else "FAIL"
        print(f"  [{status}] {result.name:<{width}}  {result.detail}")  # noqa: T201
    if healthy:
        print("healthy: all checks passed")  # noqa: T201
    else:
        failed = [result.name for result in results if not result.ok]
        print(f"unhealthy: {len(failed)} failing check(s): {', '.join(failed)}")  # noqa: T201
