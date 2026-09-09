"""``modulo start`` boot orchestration (ADR 031 Decisions 1/2).

IMPORT-ORDER CONTRACT (locked by ``tests/unit/launcher/test_entry.py``):
the FIRST ``modulo`` import at module top level must be
``modulo.launcher.env_safety``, no other ``modulo`` import may appear at
module top level, and :func:`run_start`'s first action must be
``scrub_os_environment()``. A launcher-hostile variable inherited from the
calling shell (``PG*``, ``PYTHONPATH``, ``LD_*``, a poisoned trust store)
must never reach any project import or the bundled runtime. Every other
project import below is therefore lazy and happens only after the scrub.

Boot order (ADR 031 Decision 2):

1. scrub the OS environment,
2. acquire the exclusive data-dir lock (whole lifetime),
3. refuse ambient service URLs on an un-bootstrapped data dir,
4. first-boot bootstrap: 0600 secrets file, HMAC state.json, initdb,
5. spawn bundled Postgres + Redis, await health probes,
6. ensure the app database, compose + pin the config env file, build
   Settings (roles + migrations run through the promoted lifespan path via
   ``DATABASE_ADMIN_URL``),
7. spawn SAQ children once migration-head readiness is confirmed,
8. serve the API with uvicorn in-process (loopback bind, port from
   state.json), with ordered teardown on every exit path.

``--detach`` double-forks + setsid (POSIX) with logs in the data dir;
TODO(P3): the Windows service seam. Windows is refused loudly everywhere
the bundled runtime is not yet supported.
"""

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modulo.launcher import env_safety

_log = logging.getLogger(__name__)

BUNDLED_BIN_DIR_ENV = "MODULO_BUNDLED_BIN_DIR"
POSTGRES_HOST = "127.0.0.1"
APP_DB_NAME = "modulo"
SECRETS_FILENAME = "secrets.json"
PGDATA_DIRNAME = "pgdata"
LAUNCHER_LOG_FILENAME = "launcher.log"
PG_VERSION_FILE = "PG_VERSION"

# The launcher-owned public surface (consumed by the CLI group; vulture's
# dead-code gate special-cases __all__).
__all__ = [
    "APP_DB_NAME",
    "BUNDLED_BIN_DIR_ENV",
    "BootError",
    "default_data_dir",
    "resolve_bin_dir",
    "run_start",
]


class BootError(RuntimeError):
    """Raised when the launcher boot cannot proceed safely."""


def default_data_dir() -> Path:
    """Per-OS data dir root (ADR 031 Decision 6; P1a = Linux)."""
    if sys.platform == "win32":
        # TODO(P3): %PROGRAMDATA%\Modulo with icacls hardening lands at P3.
        raise BootError("The native launcher data dir is not supported on Windows yet (TODO(P3))")
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / "modulo" / "data"


def resolve_bin_dir(bin_dir: Path | None = None) -> Path:
    """Resolve the bundled-binaries directory (param > env > install default)."""
    if bin_dir is not None:
        return bin_dir
    from_env = os.environ.get(BUNDLED_BIN_DIR_ENV)
    if from_env:
        return Path(from_env)
    return Path(sys.prefix) / "bundled" / "bin"


@dataclass
class _RunState:
    """Boot/serving coordination shared by signals and the serve loop."""

    shutdown_requested: threading.Event = field(default_factory=threading.Event)
    degraded: threading.Event = field(default_factory=threading.Event)
    serve_stop: threading.Event = field(default_factory=threading.Event)
    monitor_stopper: Callable[[], None] | None = None

    def install_signal_handlers(self) -> None:
        if sys.platform == "win32":
            # TODO(P3): CTRL_BREAK_EVENT handling lands with the Windows seam.
            return

        def _handle(_signum: int, _frame: object) -> None:
            self.shutdown_requested.set()
            self.serve_stop.set()
            self.stop_monitor()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

    def stop_monitor(self) -> None:
        if self.monitor_stopper is not None:
            self.monitor_stopper()

    def bind_supervisor(self, supervisor: Any) -> None:
        self.monitor_stopper = supervisor.request_stop


def run_start(
    data_dir: Path | None = None,
    *,
    detach: bool = False,
    bin_dir: Path | None = None,
    serve: Callable[[str, int, threading.Event], None] | None = None,
) -> int:
    """Boot the single-install stack and serve the API until signalled.

    Returns 0 on a clean shutdown (SIGINT/SIGTERM), 1 on a failed or
    degraded boot. ``serve`` is the uvicorn seam (tests substitute a stub);
    it blocks until its ``stop_event`` is set.
    """
    # FIRST ACTION — before any other modulo import (ADR 031 Decision 2).
    env_safety.scrub_os_environment()
    effective_data_dir = data_dir if data_dir is not None else default_data_dir()
    if detach:
        return _start_detached(effective_data_dir, bin_dir)
    return _run_foreground(effective_data_dir, bin_dir=bin_dir, serve=serve)


def _start_detached(data_dir: Path, bin_dir: Path | None) -> int:
    """Double-fork + setsid detach; logs land in the data dir (POSIX only)."""
    if sys.platform == "win32":
        # TODO(P3): Windows service/DETACHED_PROCESS seam.
        raise BootError("--detach is not supported on Windows yet (TODO(P3))")
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    data_dir.mkdir(parents=True, exist_ok=True)
    _redirect_stdio(data_dir / LAUNCHER_LOG_FILENAME)
    return _run_foreground(data_dir, bin_dir=bin_dir, serve=None)


def _redirect_stdio(log_path: Path) -> None:  # pragma: no cover - needs a real tty teardown
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(log_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    if log_fd > 2:
        os.close(log_fd)


def _run_foreground(
    data_dir: Path,
    *,
    bin_dir: Path | None,
    serve: Callable[[str, int, threading.Event], None] | None,
) -> int:
    from modulo.launcher import initdb as initdb_module
    from modulo.launcher import secrets_file
    from modulo.launcher import state as state_module
    from modulo.launcher.supervisor import DataDirLock, Supervisor, knobs_from_env, reconcile_orphans

    run_state = _RunState()
    run_state.install_signal_handlers()
    data_dir.mkdir(parents=True, exist_ok=True)
    effective_bin_dir = resolve_bin_dir(bin_dir)

    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    supervisor: Supervisor | None = None
    try:
        env_safety.assert_no_ambient_service_urls(data_dir)
        secrets = secrets_file.load_or_create(data_dir / SECRETS_FILENAME)
        launcher_state = _load_or_init_state(data_dir, secrets, state_module)
        pgdata = data_dir / PGDATA_DIRNAME
        reconcile_orphans(pgdata)
        if not (pgdata / PG_VERSION_FILE).exists():
            initdb_module.bootstrap_pgdata(pgdata, password=secrets.postgres_password, bin_dir=effective_bin_dir)

        knobs = knobs_from_env()
        supervisor = Supervisor(
            knobs,
            runtime_path=data_dir / "runtime.json",
            on_degraded=_degraded_callback(run_state),
        )
        run_state.bind_supervisor(supervisor)
        composed = _compose_and_pin_config(data_dir, launcher_state, secrets)
        supervisor.add(_postgres_child(launcher_state, pgdata, effective_bin_dir, knobs))
        supervisor.add(_redis_child(launcher_state, secrets, effective_bin_dir, composed))
        supervisor.start(start_monitor=True)

        _await_health(
            {
                "postgres": _pg_isready_probe(effective_bin_dir, launcher_state.postgres_port),
                "redis": _redis_ping_probe(effective_bin_dir, launcher_state.redis_port, secrets.redis_password),
            },
            timeout=knobs.health_check_timeout,
            interval=knobs.health_check_interval,
            stop=run_state.shutdown_requested,
        )
        _ensure_app_database(launcher_state, secrets)
        _prepare_database_env(composed)
        settings = _build_settings()

        _add_saq_children(supervisor, settings, composed)
        if run_state.shutdown_requested.is_set():
            supervisor.shutdown()
            return 0

        effective_serve = serve if serve is not None else _serve_api
        effective_serve(POSTGRES_HOST, launcher_state.api_port, run_state.serve_stop)
        supervisor.shutdown()
        if run_state.degraded.is_set() or supervisor.degraded_reason is not None:
            sys.stderr.write(
                "launcher degraded: "
                + (supervisor.degraded_reason or "crash cap tripped")
                + "\nRun `modulo doctor` for diagnosis and repair hints (ADR 031).\n"
            )
            return 1
        return 0
    except BaseException:
        if supervisor is not None:
            supervisor.shutdown()
        raise
    finally:
        lock.release()


def _degraded_callback(run_state: _RunState) -> Callable[[str], None]:
    def _on_degraded(_reason: str) -> None:
        run_state.degraded.set()
        run_state.serve_stop.set()

    return _on_degraded


def _load_or_init_state(data_dir: Path, secrets: Any, state_module: Any) -> Any:
    path = data_dir / state_module.STATE_FILENAME
    if path.exists():
        return state_module.load_state(path, secrets.state_hmac_key)
    fresh = state_module.initial_state()
    state_module.save_state(fresh, path, secrets.state_hmac_key)
    return fresh


def _compose_and_pin_config(data_dir: Path, launcher_state: Any, secrets: Any) -> dict[str, str]:
    """Compose the launcher config, write it (0600) and pin it for Settings."""
    from modulo.launcher.config_source import compose_config, write_pinned_env_file
    from modulo.settings import pin_env_file, set_first_boot_guard

    config_path = write_pinned_env_file(data_dir, launcher_state, secrets)
    pin_env_file(str(config_path))
    set_first_boot_guard(env_safety.make_first_boot_guard(data_dir, env_file=config_path))
    return compose_config(launcher_state, secrets)


def _prepare_database_env(composed: dict[str, str]) -> None:
    """Export the admin/system URLs the lifespan-equivalent boot steps read."""
    admin_url = composed.get("DATABASE_ADMIN_URL")
    if admin_url:
        os.environ["DATABASE_ADMIN_URL"] = admin_url
    system_url = composed.get("MODULO_SYSTEM_DATABASE_URL")
    if system_url:
        os.environ["MODULO_SYSTEM_DATABASE_URL"] = system_url


def _build_settings() -> Any:
    from modulo.settings import get_settings

    return get_settings()


def _add_saq_children(supervisor: Any, settings: Any, composed: dict[str, str]) -> None:
    """Register the SAQ workers; they spawn only at migration-head readiness."""
    from modulo.api.dependencies import get_or_create_engine
    from modulo.db.health_checks import db_is_at_migration_head
    from modulo.launcher.supervisor import ChildSpec

    engine = get_or_create_engine(settings)
    head_cache: list[bool | None] = [None]

    def _head_ready() -> bool:
        if head_cache[0] is None:
            import asyncio

            head_cache[0] = asyncio.run(db_is_at_migration_head(engine))
        return bool(head_cache[0])

    def _child_env(base: dict[str, str]) -> dict[str, str]:
        env = dict(base)
        for key in ("DATABASE_URL", "DATABASE_ADMIN_URL", "REDIS_URL", "MODULO_SYSTEM_DATABASE_URL"):
            if composed.get(key):
                env[key] = composed[key]
        return env

    for name, module_argv in (
        ("saq-runs", [sys.executable, "-m", "saq", "modulo.core.saq_worker.runs_settings"]),
        ("saq-system", [sys.executable, "-m", "modulo.core.saq_worker"]),
    ):

        def _build_argv(argv: list[str] = module_argv) -> list[str]:
            return list(argv)

        supervisor.add(
            ChildSpec(
                name=name,
                argv_builder=_build_argv,
                start_condition=_head_ready,
                env_builder=_child_env,
                shutdown_priority=0,
            )
        )


# ---------------------------------------------------------------------------
# Bundled-service children + probes
# ---------------------------------------------------------------------------


def _postgres_child(launcher_state: Any, pgdata: Path, bin_dir: Path, knobs: Any) -> Any:
    from modulo.launcher.initdb import postgres_server_argv
    from modulo.launcher.supervisor import ChildSpec

    return ChildSpec(
        name="postgres",
        argv_builder=lambda: postgres_server_argv(pgdata, POSTGRES_HOST, launcher_state.postgres_port, bin_dir),
        probe=_pg_isready_probe(bin_dir, launcher_state.postgres_port),
        shutdown_priority=1,
        teardown_signal=int(signal.SIGINT),
        shutdown_escalation_timeout=knobs.pg_fast_shutdown_timeout,
    )


def _redis_child(launcher_state: Any, secrets: Any, bin_dir: Path, composed: dict[str, str]) -> Any:
    from modulo.launcher.supervisor import ChildSpec

    redis_url = composed.get("REDIS_URL", "")

    def _child_env(base: dict[str, str]) -> dict[str, str]:
        env = dict(base)
        if redis_url:
            env["REDIS_URL"] = redis_url
        return env

    return ChildSpec(
        name="redis",
        argv_builder=lambda: _redis_server_argv(bin_dir, launcher_state.redis_port, secrets.redis_password),
        probe=_redis_ping_probe(bin_dir, launcher_state.redis_port, secrets.redis_password),
        env_builder=_child_env,
        shutdown_priority=2,
    )


def _redis_server_argv(bin_dir: Path, port: int, password: str) -> list[str]:
    from modulo.launcher.initdb import _binary

    return [
        _binary(bin_dir, "redis-server"),
        "--port",
        str(port),
        "--bind",
        POSTGRES_HOST,
        "--requirepass",
        password,
        "--save",
        "",
        "--appendonly",
        "no",
    ]


def _run_probe_command(argv: list[str], *, env: dict[str, str] | None = None, expect: str | None = None) -> bool:
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603 — fully pinned probe argv
            argv,
            capture_output=True,
            timeout=5,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    if expect is None:
        return True
    return expect.encode() in result.stdout


def _pg_isready_probe(bin_dir: Path, port: int) -> Callable[[], bool]:
    from modulo.launcher.initdb import _binary

    argv = [_binary(bin_dir, "pg_isready"), "-h", POSTGRES_HOST, "-p", str(port)]

    def _probe() -> bool:
        return _run_probe_command(argv)

    return _probe


def _redis_ping_probe(bin_dir: Path, port: int, password: str) -> Callable[[], bool]:
    from modulo.launcher.initdb import _binary

    argv = [_binary(bin_dir, "redis-cli"), "-h", POSTGRES_HOST, "-p", str(port), "PING"]
    env = {"REDISCLI_AUTH": password}

    def _probe() -> bool:
        return _run_probe_command(argv, env=env, expect="PONG")

    return _probe


def _await_health(
    probes: dict[str, Callable[[], bool]],
    *,
    timeout: float,
    interval: float,
    stop: threading.Event,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until every bundled service answers its probe (or fail the boot)."""
    pending = dict(probes)
    deadline = clock() + timeout
    while pending:
        if stop.is_set():
            raise BootError("shutdown requested while waiting for the bundled services")
        for name, probe in list(pending.items()):
            try:
                healthy = probe()
            except Exception:
                _log.exception("entry.probe_error name=%s", name)
                healthy = False
            if healthy:
                pending.pop(name)
        if not pending:
            return
        if clock() >= deadline:
            raise BootError(f"bundled services failed to become healthy within {timeout:.0f}s: {sorted(pending)}")
        sleep(interval)


def _ensure_app_database(launcher_state: Any, secrets: Any) -> None:
    """Create the app database inside the bundled cluster (idempotent)."""
    import asyncio

    import asyncpg

    async def _create() -> None:
        root_url = (
            f"postgresql://modulo:{secrets.postgres_password}@{POSTGRES_HOST}:{launcher_state.postgres_port}/postgres"
        )
        conn = await asyncpg.connect(root_url, ssl=False)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", APP_DB_NAME)
            if not exists:
                await conn.execute(f"CREATE DATABASE {APP_DB_NAME}")
        finally:
            await conn.close()

    asyncio.run(_create())


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def _serve_api(host: str, port: int, stop_event: threading.Event) -> None:
    """Serve the API with uvicorn in-process; the entry owns signal handling."""
    import uvicorn

    from modulo.api.main import app

    config = uvicorn.Config(app, host=host, port=port, server_header=False)
    server = uvicorn.Server(config)
    # The entry owns signal handling (SIGINT/SIGTERM must drive the ordered
    # teardown, not uvicorn's own handler).
    server.install_signal_handlers = _noop_signal_handlers  # type: ignore[attr-defined]
    watcher = threading.Thread(target=_serve_watch, args=(server, stop_event), daemon=True)
    watcher.start()
    server.run()


def _noop_signal_handlers() -> None:  # pragma: no cover - trivial seam
    return None


def _serve_watch(server: Any, stop_event: threading.Event) -> None:
    stop_event.wait()
    if server.should_exit:
        return  # already exiting (degraded teardown won that race)
    server.should_exit = True
