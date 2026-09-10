"""``modulo start`` boot orchestration (ADR 031 Decisions 1/2).

IMPORT-ORDER CONTRACT (locked by ``tests/unit/launcher/test_entry.py``):
the FIRST ``modulo`` import at module top level must be
``modulo.launcher.env_safety``, no other ``modulo`` import may appear at
module top level, and :func:`run_start`'s first action must be
``scrub_os_environment()``. A launcher-hostile variable inherited from the
calling shell (``PG*``, ``PYTHONPATH``, ``LD_*``, a poisoned trust store)
must never reach any project import or the bundled runtime. Every other
project import below is therefore lazy and happens only after the scrub.
The PUBLISHED console script (``modulo.cli.main``) also scrubs at its own
module import — BEFORE it imports ``modulo.cli.backup`` (psycopg, settings,
apply) — so the backup/restore path is covered on the real entry point too
(locked by the import-hygiene subprocess test).

Platform: the bundled runtime is LINUX-FIRST. macOS is refused loudly at
boot — the child-death shim depends on /proc STARTTIME, which only Linux
provides (TODO(P2) macOS variant); Windows carries the TODO(P3) seam.

Boot order (ADR 031 Decision 2):

1. scrub the OS environment,
2. acquire the exclusive data-dir lock (whole lifetime),
3. verify the bundled binaries exist (fail fast with a --bin-dir hint),
4. first-boot bootstrap: 0600 secrets file, HMAC state.json, initdb,
5. spawn bundled Postgres + Redis, await health probes,
6. ensure the app database, compose + pin the config env file, build
   Settings (roles + migrations run through the promoted lifespan path via
   ``DATABASE_ADMIN_URL``),
7. spawn SAQ children once migration-head readiness is confirmed,
8. serve the API with uvicorn in-process (loopback bind, port from
   state.json), with ordered teardown on every exit path.

``--detach`` double-forks + setsid (POSIX) with logs in the data dir; the
intermediate fork waits for a readiness handshake from the detached
grandchild and forwards boot failures to the original stderr BEFORE the
caller's process exits, so a failed detached boot can never surface as a
silent success. TODO(P3): the Windows service seam.

Terminal degraded (FAR-674): a crash-cap trip is TERMINAL. The supervisor
persists the degrade record (``degraded.json`` sibling of state.json —
reason + the last crash backtraces) and the launcher exits with
``policy.DEGRADED_EXIT_CODE``; a subsequent normal start REFUSES with the
persisted reason and the resume path until ``modulo start --clear-degraded``
(or ``modulo clear-degraded``) clears the record. When the degrade happens
within ``policy.UPGRADE_DEGRADED_WINDOW_SECONDS`` of a recorded upgrade
boot (the ``upgrade.json`` marker FAR-675's machinery writes), the exit
and refusal messages additionally carry the refuse/restore guidance and the
pre-upgrade snapshot path.
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modulo.launcher.supervisor import ProbeOutcome

from modulo.launcher import env_safety

_log = logging.getLogger(__name__)

BUNDLED_BIN_DIR_ENV = "MODULO_BUNDLED_BIN_DIR"
POSTGRES_HOST = "127.0.0.1"
APP_DB_NAME = "modulo"
SECRETS_FILENAME = "secrets.json"
PGDATA_DIRNAME = "pgdata"
LAUNCHER_LOG_FILENAME = "launcher.log"
PG_VERSION_FILE = "PG_VERSION"

# Bound on how long the --detach handshake waits for the grandchild's
# readiness signal (first boot runs initdb, which can take minutes).
_DETACH_HANDSHAKE_TIMEOUT_SECONDS = 300.0

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
    force_exit: threading.Event = field(default_factory=threading.Event)
    signal_count: int = 0
    monitor_stopper: Callable[[], None] | None = None

    def install_signal_handlers(self) -> None:
        if sys.platform == "win32":
            # TODO(P3): CTRL_BREAK_EVENT handling lands with the Windows seam.
            return

        def _handle(_signum: int, _frame: object) -> None:
            self.signal_count += 1
            self.shutdown_requested.set()
            self.serve_stop.set()
            if self.signal_count >= 2:
                # A second Ctrl-C means the operator is done waiting for
                # the graceful drain: uvicorn must force-exit.
                self.force_exit.set()
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
    clear_degraded: bool = False,
    serve: Callable[[str, int, threading.Event], None] | None = None,
) -> int:
    """Boot the single-install stack and serve the API until signalled.

    Returns ``policy.DEGRADED_EXIT_CODE`` on a terminal-degraded exit, 0 on
    a clean shutdown (SIGINT/SIGTERM), 1 on a failed boot. ``serve`` is the
    uvicorn seam (tests substitute a stub); it blocks until its
    ``stop_event`` is set. ``clear_degraded`` removes a persisted
    terminal-degraded record before the boot (the resume path).
    """
    # FIRST ACTION — before any other modulo import (ADR 031 Decision 2).
    env_safety.scrub_os_environment()
    if sys.platform == "darwin":
        raise BootError(
            "The native launcher requires Linux: the child-death watchdog depends on "
            "/proc STARTTIME, which macOS does not provide (a bundled service would be "
            "killed moments after every spawn). macOS support is planned (P2) — use the "
            "Docker Compose path meanwhile."
        )
    effective_data_dir = data_dir if data_dir is not None else default_data_dir()
    if detach:
        return _start_detached(effective_data_dir, bin_dir, clear_degraded=clear_degraded)
    return _run_foreground(effective_data_dir, bin_dir=bin_dir, clear_degraded=clear_degraded, serve=serve)


def _start_detached(data_dir: Path, bin_dir: Path | None, *, clear_degraded: bool = False) -> int:
    """Double-fork + setsid detach; logs land in the data dir (POSIX only).

    The caller must NOT be told "success" before the detached boot has
    proven itself: the intermediate fork keeps the original stderr and a
    pipe, waits for the grandchild's readiness handshake (the grandchild
    writes ``ready`` once the boot reached the serve phase, or ``failed``
    with the error text), and forwards the outcome so the CLI's exit code
    reflects reality.
    """
    if sys.platform == "win32":
        # TODO(P3): Windows service/DETACHED_PROCESS seam.
        raise BootError("--detach is not supported on Windows yet (TODO(P3))")
    read_fd, write_fd = os.pipe()
    first_pid = os.fork()
    if first_pid > 0:
        os.close(read_fd)
        os.close(write_fd)
        _, status = os.waitpid(first_pid, 0)
        os._exit(0 if status == 0 else 1)
    os.setsid()
    second_pid = os.fork()
    if second_pid > 0:
        os.close(write_fd)
        outcome = _await_detached_handshake(read_fd)
        if outcome is None:
            os._exit(0)
        sys.stderr.write(outcome + "\n")
        sys.stderr.flush()
        os._exit(1)
    os.close(read_fd)
    data_dir.mkdir(parents=True, exist_ok=True)
    _redirect_stdio(data_dir / LAUNCHER_LOG_FILENAME)
    try:
        code = _run_foreground(data_dir, bin_dir=bin_dir, clear_degraded=clear_degraded, serve=None, ready_fd=write_fd)
    except BaseException as exc:
        message = f"failed: {exc}".encode()
        with contextlib.suppress(OSError):
            os.write(write_fd, message)
        # Also reach the (possibly redirected) stderr so the launcher log
        # carries the failure even when no handshake reader exists.
        sys.stderr.write(f"detached boot failed: {exc}\n")
        sys.stderr.flush()
        os.close(write_fd)
        return 1
    os.close(write_fd)
    return code


def _await_detached_handshake(read_fd: int) -> str | None:
    """Read the detached boot's handshake (None = ready; str = the failure).

    Blocks until a newline-terminated line arrives or EOF. A hard crash
    (no ``failed`` line, just EOF) is reported with the launcher-log path
    so the operator knows where to look.
    """
    deadline = time.monotonic() + _DETACH_HANDSHAKE_TIMEOUT_SECONDS
    buffer = bytearray()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([read_fd], [], [], min(remaining, 1.0))
        if not ready:
            continue
        chunk = os.read(read_fd, 4096)
        if not chunk:
            return "detached boot died before signalling readiness — check launcher.log in the data dir"
        buffer.extend(chunk)
        if b"\n" in buffer:
            line = bytes(buffer).split(b"\n", 1)[0].decode(errors="replace")
            return None if line == "ready" else f"detached boot failed: {line.removeprefix('failed: ')}"
    return (
        f"detached boot did not signal readiness within {_DETACH_HANDSHAKE_TIMEOUT_SECONDS:.0f}s — "
        "it may still be booting (initdb is slow on first boot) or stalled; check launcher.log in the data dir"
    )


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
    clear_degraded: bool = False,
    serve: Callable[[str, int, threading.Event], None] | None,
    ready_fd: int | None = None,
) -> int:
    import time as time_module

    from modulo.launcher import initdb as initdb_module
    from modulo.launcher import policy, secrets_file
    from modulo.launcher import state as state_module
    from modulo.launcher.supervisor import (
        DEGRADED_FILENAME,
        UPGRADE_MARKER_FILENAME,
        DataDirLock,
        LauncherError,
        Supervisor,
        clear_degraded_record,
        knobs_from_env,
        read_degraded_record,
        reconcile_orphans,
    )

    run_state = _RunState()
    run_state.install_signal_handlers()
    data_dir.mkdir(parents=True, exist_ok=True)
    effective_bin_dir = resolve_bin_dir(bin_dir)
    degraded_path = data_dir / DEGRADED_FILENAME

    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    supervisor: Supervisor | None = None
    try:
        try:
            # Terminal-degraded gate (FAR-674): the resume path clears the
            # record first; a present record refuses the normal start.
            if clear_degraded:
                clear_degraded_record(degraded_path)
                _log.info("entry.degraded_state_cleared path=%s", degraded_path)
            degraded_record = read_degraded_record(degraded_path)
            if degraded_record is not None:
                raise BootError(_degraded_refusal_message(degraded_record, degraded_path, data_dir))
            env_safety.assert_no_ambient_service_urls(data_dir)
            _verify_bundled_binaries(effective_bin_dir)
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
                degraded_path=degraded_path,
                degraded_context=_upgrade_context_provider(
                    data_dir / UPGRADE_MARKER_FILENAME, time_module.time, policy.UPGRADE_DEGRADED_WINDOW_SECONDS
                ),
            )
            run_state.bind_supervisor(supervisor)
            composed = _compose_and_pin_config(data_dir, launcher_state, secrets)
            supervisor.add(_postgres_child(launcher_state, pgdata, effective_bin_dir, knobs))
            supervisor.add(_redis_child(launcher_state, secrets, effective_bin_dir, data_dir))
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

            if ready_fd is not None:
                # Readiness handshake: the boot reached the serve phase —
                # everything before this point has succeeded.
                with contextlib.suppress(OSError):
                    os.write(ready_fd, b"ready\n")
            if serve is not None:
                serve(POSTGRES_HOST, launcher_state.api_port, run_state.serve_stop)
            else:
                _serve_api(POSTGRES_HOST, launcher_state.api_port, run_state.serve_stop, run_state.force_exit)
            monitor_abnormal = (
                not supervisor.monitor_thread_alive()
                and supervisor.degraded_reason is None
                and not run_state.shutdown_requested.is_set()
            )
            supervisor.shutdown()
            if run_state.degraded.is_set() or supervisor.degraded_reason is not None or monitor_abnormal:
                reason = supervisor.degraded_reason or "a supervisor failure was detected"
                sys.stderr.write(_degraded_exit_message(reason, degraded_path, data_dir))
                sys.stderr.flush()
                return policy.DEGRADED_EXIT_CODE
            return 0
        except (BootError, LauncherError, env_safety.AmbientEnvironmentError):
            raise
        except Exception as exc:
            # Boot failures must reach the operator as actionable text, not
            # raw tracebacks (missing binaries, bind errors, driver errors).
            raise BootError(_boot_failure_message(exc, effective_bin_dir)) from exc
    except BaseException:
        # The ORIGINAL boot error must never be masked by a teardown
        # failure, and the remaining children must still be torn down.
        if supervisor is not None:
            try:
                supervisor.shutdown()
            except Exception:
                _log.exception("supervisor.shutdown_failed during boot error handling")
        raise
    finally:
        lock.release()


def _boot_failure_message(exc: Exception, bin_dir: Path) -> str:
    """Actionable boot-failure text (the CLI surfaces it verbatim)."""
    text = str(exc) or repr(exc)
    if isinstance(exc, FileNotFoundError):
        return f"{text} (bundled binaries not found at {bin_dir} - install the bundled runtime or pass --bin-dir)"
    return text


def _verify_bundled_binaries(bin_dir: Path) -> None:
    """Fail the boot fast — with a --bin-dir hint — when binaries are missing.

    Every bundled runtime the launcher will need (initdb, postgres,
    pg_isready, redis-server, redis-cli) must exist in *bin_dir*. Verifying
    up front turns a mid-boot FileNotFoundError into one actionable
    message naming the directory to fix.
    """
    from modulo.launcher.initdb import _binary

    required = ("initdb", "postgres", "pg_isready", "redis-server", "redis-cli")
    missing = [name for name in required if not Path(_binary(bin_dir, name)).exists()]
    if missing:
        raise BootError(
            f"bundled binaries not found at {bin_dir} (missing: {', '.join(missing)}) "
            "- install the bundled runtime or pass --bin-dir"
        )


def _degraded_callback(run_state: _RunState) -> Callable[[str], None]:
    def _on_degraded(_reason: str) -> None:
        run_state.degraded.set()
        run_state.serve_stop.set()

    return _on_degraded


# ---------------------------------------------------------------------------
# Terminal-degraded messaging + post-upgrade context (FAR-674)
# ---------------------------------------------------------------------------


def _degraded_exit_message(reason: str, degraded_path: Path, data_dir: Path) -> str:
    """The terminal-degraded stderr text (actionable, names the resume path)."""
    from modulo.launcher.supervisor import read_degraded_record

    lines = [
        f"launcher degraded: {reason}",
        "This is a TERMINAL degraded state: the launcher exited without restarting its stack.",
        f"Inspect launcher.log in {data_dir} and the persisted crash backtraces in",
        f"{degraded_path}; run `modulo doctor` for a per-check diagnosis and `modulo status`",
        "for the component table.",
        "After fixing the cause, resume with `modulo start --clear-degraded`.",
    ]
    guidance = _post_upgrade_guidance(read_degraded_record(degraded_path))
    if guidance:
        lines.append(guidance)
    return "\n".join(lines) + "\n"


def _degraded_refusal_message(record: dict[str, Any], degraded_path: Path, data_dir: Path) -> str:
    """Why a normal start refuses while terminal-degraded, plus the resume path."""
    import time as time_module

    reason = record.get("reason", "unknown")
    lines = [
        "Refusing to start: the launcher is in a TERMINAL DEGRADED state (a crash-cap trip",
        f"was persisted for this data dir). Reason: {reason}",
    ]
    degraded_at = record.get("degraded_at")
    if isinstance(degraded_at, (int, float)) and not isinstance(degraded_at, bool):
        stamp = time_module.strftime("%Y-%m-%dT%H:%M:%SZ", time_module.gmtime(float(degraded_at)))
        lines.append(f"Degraded at: {stamp}")
    lines.extend(
        [
            f"The last crash backtraces are persisted in {degraded_path}; the launcher log",
            f"is {data_dir / 'launcher.log'}.",
            "Inspect the cause (`modulo doctor`, `modulo status`), fix it, then resume with",
            "`modulo start --clear-degraded` (or `modulo clear-degraded`).",
        ]
    )
    guidance = _post_upgrade_guidance(record)
    if guidance:
        lines.append(guidance)
    return "\n".join(lines)


def _post_upgrade_guidance(record: dict[str, Any] | None) -> str:
    """Post-upgrade refuse/restore guidance (detection only; FAR-675 lands later)."""
    if not isinstance(record, dict) or not record.get("post_upgrade"):
        return ""
    snapshot = record.get("pre_upgrade_snapshot")
    lines = [
        (
            "This degraded state began within the upgrade watch window "
            "(an upgrade boot was recently recorded for this data dir)."
        )
    ]
    if isinstance(snapshot, str) and snapshot:
        lines.append(
            f"A pre-upgrade snapshot was recorded at {snapshot} — restore it manually with "
            f"`modulo restore {snapshot}` (see `modulo restore --help`), or downgrade the binary "
            "to the pre-upgrade version."
        )
    else:
        lines.append(
            "No pre-upgrade snapshot was recorded — restore from your external backups, or "
            "downgrade the binary to the pre-upgrade version."
        )
    lines.append("The full upgrade/rollback machinery is the upgrade epic (FAR-675).")
    return "\n".join(lines)


def _upgrade_context_provider(
    marker_path: Path, now: Callable[[], float], window_seconds: float
) -> Callable[[], dict[str, Any]]:
    """Degrade-context provider: post-upgrade detection (read-only).

    Consumes the ``upgrade.json`` marker FAR-675's machinery writes. When a
    degrade occurs within *window_seconds* of the recorded upgrade boot the
    context marks it ``post_upgrade`` and carries the pre-upgrade snapshot
    path (if the marker recorded one) so the degrade message includes the
    refuse/restore guidance.
    """
    from modulo.launcher.supervisor import read_upgrade_marker

    def _context() -> dict[str, Any]:
        marker = read_upgrade_marker(marker_path)
        if marker is None:
            return {}
        upgraded_at = marker.get("upgraded_at")
        if not isinstance(upgraded_at, (int, float)) or isinstance(upgraded_at, bool):
            return {}
        if now() - float(upgraded_at) > window_seconds:
            return {}
        context: dict[str, Any] = {"post_upgrade": True}
        snapshot = marker.get("pre_upgrade_snapshot")
        if isinstance(snapshot, str) and snapshot:
            context["pre_upgrade_snapshot"] = snapshot
        return context

    return _context


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
    """Register the SAQ workers; they spawn only at migration-head readiness.

    The head check is memoized ONLY on a True result: on a fresh install
    the first tick can evaluate False (migrations still running in the
    lifespan), and caching that would seal the gate shut forever — the API
    would serve with zero SAQ workers. A False answer is simply re-asked on
    the next tick.
    """
    from modulo.api.dependencies import get_or_create_engine
    from modulo.db.health_checks import db_is_at_migration_head
    from modulo.launcher.supervisor import ChildSpec

    engine = get_or_create_engine(settings)
    head_cache: list[bool] = [False]
    head_checked: list[bool] = [False]

    def _head_ready() -> bool:
        if not head_checked[0]:
            import asyncio

            if asyncio.run(db_is_at_migration_head(engine)):
                head_cache[0] = True
                head_checked[0] = True
        return head_cache[0]

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


# The child environment allowlist for the bundled SERVICES: postgres and
# redis get PATH/HOME/locale/tmp plus nothing else — the operator's full
# environment (proxy vars, tool config, secrets) must not leak into the
# bundled daemons. Everything the services need is passed via argv/config.
_BUNDLED_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def _bundled_service_env(base: dict[str, str]) -> dict[str, str]:
    """Allowlisted environment for the bundled postgres/redis children."""
    return {name: base[name] for name in _BUNDLED_ENV_ALLOWLIST if name in base}


def _postgres_child(launcher_state: Any, pgdata: Path, bin_dir: Path, knobs: Any) -> Any:
    from modulo.launcher.initdb import postgres_server_argv
    from modulo.launcher.supervisor import ChildSpec

    return ChildSpec(
        name="postgres",
        argv_builder=lambda: postgres_server_argv(pgdata, POSTGRES_HOST, launcher_state.postgres_port, bin_dir),
        probe=_pg_isready_probe(bin_dir, launcher_state.postgres_port),
        env_builder=_bundled_service_env,
        shutdown_priority=1,
        teardown_signal=int(signal.SIGINT),
        shutdown_escalation_timeout=knobs.pg_fast_shutdown_timeout,
    )


def _redis_child(launcher_state: Any, secrets: Any, bin_dir: Path, data_dir: Path) -> Any:
    from modulo.launcher.supervisor import ChildSpec

    return ChildSpec(
        name="redis",
        # The password lives in a per-boot 0600 config file, NEVER on the
        # argv (argv is world-readable via /proc/<pid>/cmdline).
        argv_builder=lambda: _redis_server_argv(bin_dir, launcher_state.redis_port, secrets.redis_password, data_dir),
        probe=_redis_ping_probe(bin_dir, launcher_state.redis_port, secrets.redis_password),
        env_builder=_bundled_service_env,
        shutdown_priority=2,
    )


def _redis_server_argv(bin_dir: Path, port: int, password: str, data_dir: Path) -> list[str]:
    """Build the redis-server invocation: config file carries the password."""
    from modulo.launcher.initdb import _binary

    conf_path = _write_redis_conf(data_dir, port, password)
    return [
        _binary(bin_dir, "redis-server"),
        str(conf_path),
    ]


def _write_redis_conf(data_dir: Path, port: int, password: str) -> Path:
    """Write the per-boot 0600 Redis config (requirepass INSIDE the file).

    Redis reads ``requirepass`` from the config; passing it as ``--requirepass``
    instead would publish the password on the world-readable
    ``/proc/<pid>/cmdline``. The file is written atomically via the promoted
    exclusive-create writer and swept with the other SIGKILL debris.
    """
    import secrets as secrets_module

    from modulo.db.bootstrap import _write_env_file
    from modulo.launcher.supervisor import REDIS_CONF_PREFIX

    content = "\n".join(
        [
            f"port {port}",
            f"bind {POSTGRES_HOST}",
            f"requirepass {password}",
            'save ""',
            "appendonly no",
            "",
        ]
    )
    conf_path = data_dir / f"{REDIS_CONF_PREFIX}{secrets_module.token_hex(8)}"
    _write_env_file(str(conf_path), content)
    return conf_path


def _run_probe_command(
    argv: list[str], *, env: dict[str, str] | None = None, expect: str | None = None
) -> bool | ProbeOutcome:
    """Run one probe binary (tri-state: healthy / unhealthy / UNAVAILABLE).

    A missing binary, an unexecutable path or a probe timeout means the
    probe TOOL could not answer — never that the child is down — so it maps
    to :attr:`ProbeOutcome.UNAVAILABLE` and the supervisor leaves the child
    alone.
    """
    import subprocess

    from modulo.launcher.supervisor import ProbeOutcome

    try:
        result = subprocess.run(  # noqa: S603 — fully pinned probe argv
            argv,
            capture_output=True,
            timeout=5,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return ProbeOutcome.UNAVAILABLE
    except subprocess.TimeoutExpired:
        return ProbeOutcome.UNAVAILABLE
    except OSError:
        return ProbeOutcome.UNAVAILABLE
    if result.returncode != 0:
        return False
    if expect is None:
        return True
    return expect.encode() in result.stdout


def _pg_isready_probe(bin_dir: Path, port: int) -> Callable[[], bool | ProbeOutcome]:
    from modulo.launcher.initdb import _binary

    argv = [_binary(bin_dir, "pg_isready"), "-h", POSTGRES_HOST, "-p", str(port)]

    def _probe() -> bool | ProbeOutcome:
        return _run_probe_command(argv)

    return _probe


def _redis_ping_probe(bin_dir: Path, port: int, password: str) -> Callable[[], bool | ProbeOutcome]:
    from modulo.launcher.initdb import _binary

    argv = [_binary(bin_dir, "redis-cli"), "-h", POSTGRES_HOST, "-p", str(port), "PING"]
    env = {"REDISCLI_AUTH": password}

    def _probe() -> bool | ProbeOutcome:
        return _run_probe_command(argv, env=env, expect="PONG")

    return _probe


def _await_health(
    probes: dict[str, Callable[[], bool | ProbeOutcome]],
    *,
    timeout: float,
    interval: float,
    stop: threading.Event,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until every bundled service answers its probe (or fail the boot)."""
    from modulo.launcher.supervisor import ProbeOutcome

    pending = dict(probes)
    deadline = clock() + timeout
    while pending:
        if stop.is_set():
            raise BootError("shutdown requested while waiting for the bundled services")
        for name, probe in list(pending.items()):
            try:
                outcome = probe()
            except Exception:
                _log.exception("entry.probe_error name=%s", name)
                outcome = ProbeOutcome.UNAVAILABLE
            if outcome is True or outcome is ProbeOutcome.HEALTHY:
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


def _serve_api(host: str, port: int, stop_event: threading.Event, force_exit: threading.Event) -> None:
    """Serve the API with uvicorn in-process; the entry owns signal handling.

    A first Ctrl-C starts the graceful drain; a SECOND one (``force_exit``)
    skips the drain — an operator must always be able to force-exit.
    """
    import uvicorn

    from modulo.api.main import app

    config = uvicorn.Config(app, host=host, port=port, server_header=False)
    server = uvicorn.Server(config)
    # The entry owns signal handling (SIGINT/SIGTERM must drive the ordered
    # teardown, not uvicorn's own handler).
    server.install_signal_handlers = _noop_signal_handlers  # type: ignore[attr-defined]
    watcher = threading.Thread(target=_serve_watch, args=(server, stop_event, force_exit), daemon=True)
    watcher.start()
    server.run()


def _noop_signal_handlers() -> None:  # pragma: no cover - trivial seam
    return None


def _serve_watch(server: Any, stop_event: threading.Event, force_exit: threading.Event) -> None:
    stop_event.wait()
    if server.should_exit:
        return  # already exiting (degraded teardown won that race)
    server.should_exit = True
    if force_exit.wait(timeout=5):
        server.force_exit = True
