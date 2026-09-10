"""In-app supervisor + data-dir lock (ADR 031 Decisions 5).

The supervisor owns the bundled Postgres/Redis AND the SAQ worker children —
all as child processes of the launcher process. The OS service manager (P1b)
owns only the launcher's own restart, never a child's.

Contracts locked by tests (``tests/unit/launcher/test_supervisor.py``):

* **Exclusive data-dir lock**: the launcher holds an OS-advisory
  ``flock`` on the ``<datadir>.lock`` sibling file for its WHOLE lifetime.
  The kernel releases it on process death, so a SIGKILLed launcher can never
  wedge the data dir. A concurrent start refuses and names the holder's
  PID + mode; the holder's /proc STARTTIME is recorded at acquire so
  ``request_stop`` can verify the identity before signalling (a recycled
  PID is never SIGTERMed) and a clean release truncates the holder record.
  POSIX-only: Windows carries the TODO(P3) Job-Object seam and refuses
  loudly.
* **Restart backoff + sliding-window crash cap**: a crashed/unhealthy child
  is restarted with exponential backoff (initial → max); crash timestamps
  land in a sliding window and reaching the cap (default 5 crashes within
  10 minutes — the cap trips ON the Nth crash, ``>=``) trips the terminal
  degraded state: ordered teardown, nonzero exit, and the ``modulo status``
  /launcher-log repair hint. A CLEAN exit (code 0) is not a crash: it does
  not feed the window (the window is cleared — a service that ran long
  enough to exit cleanly proves its crashes were transient), the crash
  hook is not called, and the child is still respawned. Counters arm at
  spawn; deferred children (SAQ behind migration-head readiness) cannot
  crash before their start condition is met because they are not spawned
  until it is.
* **Child shim owns death**: every supervised child ON LINUX runs under a
  shim process (``python -m modulo.launcher.supervisor child-shim``) that
  polls the launcher's ``/proc/<ppid>/stat`` STARTTIME (field 22) at most
  every 2 seconds — PID-reuse safe — and kills the child when the parent is
  gone. On Linux the child additionally carries PDEATHSIG (pre-exec) as a
  belt-and-braces optimisation. Off Linux the shim has no readable
  STARTTIME, so the argv passes through UNWRAPPED (``read_proc_starttime``
  returns None everywhere but Linux; a wrapped shim would kill its child on
  the first watchdog tick) and the entry refuses non-Linux platforms loudly
  until the platform-native death-binding lands (TODO(P2) macOS,
  TODO(P3) Windows Job Objects).
* **Probe tri-state + startup deadline**: a probe answers healthy,
  unhealthy, or PROBE-UNAVAILABLE (the probe TOOL itself is broken — e.g.
  the binary is missing). PROBE-UNAVAILABLE never terminates a child: the
  healthy flag is left untouched and the failure is logged once per child
  until a real outcome arrives. A child that never becomes healthy is not
  re-probed forever: ``spawn_time + health_check_timeout`` is the startup
  deadline — past it the child is terminated and its exit feeds the normal
  crash/backoff path. Children WITHOUT a probe (the SAQ workers) get the
  same deadline as a liveness contract: surviving the window without dying
  marks them healthy (their death remains the crash signal).
* **Ordered teardown on every exit path**: SAQ children → Postgres
  (fast shutdown via SIGINT to the process group, then smart SIGTERM, then
  SIGKILL) → Redis. Ctrl-C exits 0 with all children reaped.
  TODO(P3): Windows CTRL_BREAK_EVENT.
* **Boot-time orphan reconciliation**: a stale ``postmaster.pid`` is removed
  ONLY when provably not a live postgres (dead PID, PID reuse, or a
  STARTTIME mismatch); a live postgres holding it is a hard refusal.
  SIGKILL debris (initdb temp dirs + plaintext pwfiles) is swept via the
  slice-1 sweep.
* **MODULO_TEST_PAUSE_AT seam**: same deterministic pattern as slice 1 — a
  registered gate name pauses the supervisor right before the cap-trip
  teardown; an unregistered value is refused. The entry scrubs the variable
  before boot (env_safety), so the seam is inert in production.

Timing knobs default to production values and are overridden directly by
tests (≥6-8x tick-vs-window margins per the repo timing lesson); the
``MODULO_LAUNCHER_*`` environment variables are the operator override seam
until Settings grows launcher knob fields.
"""

import argparse
import contextlib
import enum
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Self

from modulo.launcher.initdb import _sweep_stale_tmp_dirs
from modulo.launcher.state import STATE_FILENAME

_log = logging.getLogger(__name__)

LOCK_SUFFIX = ".lock"
RUNTIME_FILENAME = "runtime.json"
POSTMASTER_PIDFILE = "postmaster.pid"
# Per-boot 0600 Redis config files (requirepass lives INSIDE the file so the
# password never appears on the world-readable /proc cmdline). Written fresh
# each boot next to pgdata; a SIGKILLed launcher leaves the previous one
# behind, so they are swept with the other SIGKILL debris.
REDIS_CONF_PREFIX = ".redis-conf-"

# The shim polls its parent at most this often (ADR 031 Decision 5).
SHIM_POLL_SECONDS = 2.0

# Registered MODULO_TEST_PAUSE_AT gates owned by this module (same seam shape
# as initdb's; the entry scrubs the variable so a production boot is inert).
GATE_SUPERVISOR_PRE_TEARDOWN = "supervisor_pre_teardown"
_REGISTERED_GATES = frozenset({GATE_SUPERVISOR_PRE_TEARDOWN})
_PAUSE_ENV_VAR = "MODULO_TEST_PAUSE_AT"

# Shutdown priorities: lower tears down first (SAQ -> PG -> Redis).
_PRIORITY_SAQ = 0
_PRIORITY_POSTGRES = 1
_PRIORITY_REDIS = 2

# /proc helpers — exposed for the orphan-reconciliation tests (they import
# the private names directly).
# Read-only access helpers for doctor/status (FAR-676): the supervisor
# persists a degraded reason (and a crash-incident marker) next to the
# child-PID manifest when it degrades, and exposes pure readers so
# ``modulo status`` / ``modulo doctor`` can surface the degraded flag and
# crash backtraces (from the launcher/app log tail) READ-ONLY.
DEFAULT_LOG_TAIL_BYTES = 256 * 1024
ROTATE_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
ROTATE_DEFAULT_KEEP = 5
LAUNCHER_LOG_FILENAME = "launcher.log"
LOGS_DIRNAME = "logs"
APP_LOG_NAME = "app"
CHILD_LOG_NAMES = ("postgres", "redis")

__all__ = [
    "DEFAULT_LOG_TAIL_BYTES",
    "GATE_SUPERVISOR_PRE_TEARDOWN",
    "REDIS_CONF_PREFIX",
    "ROTATE_DEFAULT_KEEP",
    "ROTATE_DEFAULT_MAX_BYTES",
    "RUNTIME_FILENAME",
    "ChildSpec",
    "DataDirLock",
    "DataDirLockError",
    "LauncherError",
    "ProbeOutcome",
    "Supervisor",
    "SupervisorKnobs",
    "_children_of",
    "child_shim_argv",
    "child_shim_main",
    "collect_status",
    "knobs_from_env",
    "log_paths",
    "read_degraded_reason",
    "read_log_tail",
    "read_proc_starttime",
    "read_runtime_manifest",
    "reconcile_orphans",
    "request_stop",
    "rotate_log",
    "write_runtime_manifest",
]


class LauncherError(RuntimeError):
    """Base class for launcher failures surfaced to the CLI."""


class DataDirLockError(LauncherError):
    """Raised when another launcher holds the exclusive data-dir lock."""


class ProbeOutcome(enum.Enum):
    """Tri-state probe result.

    ``UNAVAILABLE`` means the probe TOOL could not produce an answer (the
    binary is missing, the invocation failed, the probe timed out) — it is
    NOT evidence that the child is down and must never terminate a healthy
    child or feed the crash cap.
    """

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "probe_unavailable"


class ChildProcess(Protocol):
    """The duck-typed process handle the supervisor drives (Popen-compatible)."""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ChildSpawner = Callable[[list[str], dict[str, str] | None], ChildProcess]


@dataclass(frozen=True)
class SupervisorKnobs:
    """Supervisor timing knobs (production defaults; tests shrink them).

    Tick-vs-reaction margins in tests follow the repo timing lesson: the
    tick is at least 6-8x smaller than the window it must observe, and the
    test runs long enough to outlast the window.

    Crash-cap semantics: ``crash_cap`` is the number of NON-CLEAN crashes
    that trips the degraded state — the cap trips ON the Nth crash
    (``>=``), so ``crash_cap=5`` degrades after the 5th crash inside the
    window. Clean exits (code 0) never count and clear the window.
    """

    tick_seconds: float = 1.0
    restart_backoff_initial: float = 1.0
    restart_backoff_max: float = 30.0
    crash_window_seconds: float = 600.0
    crash_cap: int = 5
    pg_fast_shutdown_timeout: float = 15.0
    shutdown_grace_seconds: float = 10.0
    health_check_timeout: float = 60.0
    health_check_interval: float = 0.25


# Sane upper bounds for the env-override seam: an operator typo (or a
# hostile inherited variable) must not park a knob at an absurd value —
# out-of-range values are rejected LOUDLY (warning + default) instead of
# clamped silently.
_KNOB_BOUNDS: dict[str, tuple[float, float]] = {
    "tick_seconds": (0.001, 60.0),
    "restart_backoff_initial": (0.001, 60.0),
    "restart_backoff_max": (0.001, 3600.0),
    "crash_window_seconds": (1.0, 86400.0),
    "pg_fast_shutdown_timeout": (0.001, 600.0),
    "shutdown_grace_seconds": (0.001, 600.0),
    "crash_cap": (1, 1000),
}


def knobs_from_env(env: dict[str, str] | None = None) -> SupervisorKnobs:
    """Build knobs from ``MODULO_LAUNCHER_*`` env overrides (operator seam).

    Until Settings grows launcher fields, the launcher honours these env
    overrides; unknown/invalid/out-of-bounds values fall back to the
    production default with a loud warning.
    """
    source = dict(os.environ if env is None else env)
    defaults = SupervisorKnobs()
    overrides: dict[str, Any] = {}
    for attr, var in (
        ("tick_seconds", "MODULO_LAUNCHER_TICK_SECONDS"),
        ("restart_backoff_initial", "MODULO_LAUNCHER_RESTART_BACKOFF_INITIAL"),
        ("restart_backoff_max", "MODULO_LAUNCHER_RESTART_BACKOFF_MAX"),
        ("crash_window_seconds", "MODULO_LAUNCHER_CRASH_WINDOW_SECONDS"),
        ("pg_fast_shutdown_timeout", "MODULO_LAUNCHER_PG_FAST_SHUTDOWN_TIMEOUT"),
        ("shutdown_grace_seconds", "MODULO_LAUNCHER_SHUTDOWN_GRACE_SECONDS"),
        ("crash_cap", "MODULO_LAUNCHER_CRASH_CAP"),
    ):
        raw = source.get(var)
        if not raw:
            continue
        try:
            value = float(raw) if "." in raw else int(raw)
        except ValueError:
            _log.warning("supervisor.knob_ignored name=%s value=%r", var, raw)
            continue
        if not math.isfinite(value) or value <= 0:
            _log.warning("supervisor.knob_ignored_nonpositive name=%s value=%r", var, raw)
            continue
        lower, upper = _KNOB_BOUNDS[attr]
        if not lower <= value <= upper:
            _log.warning(
                "supervisor.knob_ignored_out_of_bounds name=%s value=%r bounds=(%s, %s)",
                var,
                raw,
                lower,
                upper,
            )
            continue
        default = getattr(defaults, attr)
        overrides[attr] = float(value) if isinstance(default, float) else int(value)
    return SupervisorKnobs(**overrides)


# ---------------------------------------------------------------------------
# Data-dir lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockHolder:
    """The launcher identity recorded inside the lock file after acquiring.

    ``starttime`` is the holder's /proc STARTTIME (None off Linux): it
    lets ``request_stop`` verify the identity before signalling so a
    RECYCLED PID is never SIGTERMed.
    """

    pid: int
    mode: str
    acquired_at: float
    starttime: int | None = None


def _read_lock_holder(path: Path) -> LockHolder | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = payload.get("pid")
    mode = payload.get("mode")
    acquired_at = payload.get("acquired_at")
    starttime = payload.get("starttime")
    if not isinstance(pid, int) or not isinstance(mode, str):
        return None
    return LockHolder(
        pid=pid,
        mode=mode,
        acquired_at=acquired_at if isinstance(acquired_at, float) else 0.0,
        starttime=starttime if isinstance(starttime, int) else None,
    )


class DataDirLock:
    """Exclusive OS-advisory lock over one data dir (kernel-released).

    The lock file is the ``<datadir>.lock`` SIBLING of the data dir (outside
    it, so a data-dir reset cannot hide a live lock). Acquisition is
    ``flock(LOCK_EX | LOCK_NB)``; the kernel releases it when the holding
    process dies, which is exactly the semantics ADR 031 Decision 5 wants.
    The holder's PID + mode + /proc STARTTIME are written into the file
    after acquiring so a refused concurrent start can name the culprit and
    ``modulo stop`` can verify the identity before signalling. A clean
    release truncates the holder record — a stale identity must never
    outlive the lock.
    """

    def __init__(self, data_dir: Path, *, mode: str = "serve") -> None:
        self.data_dir = data_dir
        self.mode = mode
        self._path = data_dir.parent / (data_dir.name + LOCK_SUFFIX)
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def holder(self) -> LockHolder | None:
        return _read_lock_holder(self._path)

    def acquire(self) -> None:
        """Take the exclusive lock or refuse naming the current holder."""
        if sys.platform == "win32":
            # TODO(P3): Windows Job Objects / named-mutex lock implementation.
            raise DataDirLockError(
                "The data-dir lock is not implemented on Windows yet (TODO(P3)); "
                "the native launcher is Linux-first (ADR 031 P1a)."
            )
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            holder = _read_lock_holder(self._path)
            if holder is not None:
                # A recorded holder means a launcher already owns this data
                # dir — even when that holder is *this* process (a same-process
                # second acquire must be refused and named, not treated as a
                # mid-boot re-entrancy). Name the holder AND the mode of the
                # acquire that is being refused.
                raise DataDirLockError(
                    f"Refusing to start: data dir {self.data_dir} is locked by another launcher "
                    f"(holder PID {holder.pid}, mode {holder.mode!r}); the requested acquire "
                    f"(mode {self.mode!r}) is refused. Stop that launcher first (or remove the "
                    "stale lock file only if the holder process is confirmed dead)."
                ) from exc
            raise DataDirLockError(
                f"Refusing to start: data dir {self.data_dir} is locked by another process "
                f"({exc}). Another launcher may be mid-boot."
            ) from exc
        self._fd = fd
        holder = LockHolder(
            pid=os.getpid(),
            mode=self.mode,
            acquired_at=time.time(),
            starttime=read_proc_starttime(os.getpid()),
        )
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(
                fd,
                json.dumps(
                    {
                        "pid": holder.pid,
                        "mode": holder.mode,
                        "acquired_at": holder.acquired_at,
                        "starttime": holder.starttime,
                    }
                ).encode(),
            )
            os.fsync(fd)
        except OSError:
            self.release()
            raise

    def release(self) -> None:
        """Release the flock AND clear the holder record (idempotent).

        The file itself is never unlinked, but a clean release truncates the
        holder JSON: a stale identity (PID + starttime) must never outlive
        the lock it described, otherwise ``modulo stop`` would compare
        against a recycled PID's history forever.
        """
        if self._fd is None:
            return
        if sys.platform != "win32":
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                _log.warning("supervisor.lock_unlock_failed path=%s", self._path)
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, 0)
            os.fsync(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _lock_is_free(lock_path: Path) -> bool:
    """True when no process holds the flock on *lock_path* (probe fd only).

    Opens a FRESH descriptor and tries ``flock(LOCK_EX | LOCK_NB)``: a held
    lock fails the probe, a free lock succeeds and is immediately released.
    This is the ground truth for "has the launcher stopped" — the holder
    JSON can be stale or cleared independently of the kernel lock.
    """
    if sys.platform == "win32":
        return False
    import fcntl

    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return True


def _holder_identity_matches(holder: LockHolder) -> bool | None:
    """True/False when the recorded holder identity can be verified.

    Mirrors ``_postmaster_is_stale``: a missing current STARTTIME (process
    gone) or a mismatched one (PID reused by another process) proves the
    recorded holder is gone. ``None`` = unverifiable (recorded before the
    STARTTIME field existed, or off Linux) — the caller falls back to the
    recorded PID alone.
    """
    if holder.starttime is None:
        return None
    current = read_proc_starttime(holder.pid)
    if current is None:
        return False
    return current == holder.starttime


def request_stop(data_dir: Path, *, timeout: float = 10.0) -> int:
    """Ask the launcher holding *data_dir* to stop (the ``modulo stop`` path).

    The recorded holder's /proc STARTTIME is verified BEFORE signalling —
    a recycled PID is never SIGTERMed. Completion is detected by probing
    the flock (not the holder JSON). POSIX-only; returns 0 when the
    launcher stopped (or no live holder exists), 1 when it could not be
    confirmed stopped.
    """
    if sys.platform == "win32":
        # TODO(P3): Windows service-control seam.
        raise LauncherError("modulo stop is not supported on Windows yet (TODO(P3))")
    lock_path = data_dir.parent / (data_dir.name + LOCK_SUFFIX)
    holder = _read_lock_holder(lock_path)
    if holder is None:
        if _lock_is_free(lock_path):
            _log.info("stop.no_holder data_dir=%s", data_dir)
            return 0
        raise LauncherError(
            f"Refusing to stop: the data dir {data_dir} is locked but no holder is recorded "
            "(a launcher may be mid-boot) — retry in a moment."
        )
    identity = _holder_identity_matches(holder)
    if identity is False:
        _log.warning("stop.stale_holder_identity pid=%s", holder.pid)
        if _lock_is_free(lock_path):
            return 0
        raise LauncherError(
            f"Refusing to stop: the recorded launcher PID {holder.pid} was reused by another "
            "process and the lock is still held by an unidentified process — inspect the "
            "data dir manually before signalling anything."
        )
    try:
        os.kill(holder.pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0 if _lock_is_free(lock_path) else _raise_still_locked(holder.pid, timeout)
    except OSError as exc:
        raise LauncherError(f"could not signal launcher PID {holder.pid}: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _lock_is_free(lock_path):
            return 0
        try:
            os.kill(holder.pid, 0)
        except OSError:
            if _lock_is_free(lock_path):
                return 0
            return 1
        time.sleep(0.2)
    # The holder was signalled but the kernel lock is still held at the
    # deadline: we could not *confirm* a clean stop (the holder may be
    # unverifiable, e.g. a legacy record with no STARTTIME). Per the contract
    # this is a "could not confirm stopped" outcome, reported as 1 — never an
    # exception, which is reserved for outright refusals (mid-boot / PID reuse).
    _log.warning("stop.unconfirmed pid=%s timeout=%s", holder.pid, timeout)
    return 1


def _raise_still_locked(pid: int, timeout: float) -> int:
    """The signalled holder is gone but the lock is still held: report 1."""
    _log.warning("stop.holder_gone_lock_held pid=%s timeout=%s", pid, timeout)
    return 1


# ---------------------------------------------------------------------------
# /proc helpers (POSIX shim + reconciliation)
# ---------------------------------------------------------------------------


def read_proc_starttime(pid: int) -> int | None:
    """Return the process STARTTIME (clock ticks since boot, stat field 22).

    ``/proc/<pid>/stat`` embeds the comm field which may contain spaces and
    parentheses, so everything up to the LAST ``)`` is skipped before
    counting fields. Returns None when the process does not exist.
    """
    if sys.platform == "win32":
        # TODO(P3): Windows exposes no /proc; the P3 shim uses Job Objects.
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, ValueError):
        return None
    tail = raw.rpartition(")")[2].split()
    # tail[0] is stat field 3 (state); starttime is field 22 → index 19.
    if len(tail) < 20:
        return None
    try:
        return int(tail[19])
    except ValueError:
        return None


def _children_of(pid: int) -> list[int]:
    """Direct child PIDs of *pid* (Linux /proc contract; empty elsewhere)."""
    if sys.platform != "linux":
        return []
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
    except (OSError, ValueError):
        return []
    try:
        return [int(token) for token in raw.split()]
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# Child shim (owns death detection)
# ---------------------------------------------------------------------------


def child_shim_argv(argv: list[str], *, parent_pid: int | None = None) -> list[str]:
    """Wrap *argv* in the child-shim invocation (the supervisor's spawn unit).

    The shim process is the direct parent of the real service process; it
    watches the launcher (its own parent) and kills the service when the
    launcher dies — PID-reuse safe via the STARTTIME comparison. The shim
    is only viable where ``/proc/<pid>/stat`` is readable, i.e. LINUX: on
    every other platform ``read_proc_starttime`` returns None and a wrapped
    shim would kill its freshly spawned service on the first watchdog tick
    (guaranteed crash loop), so the argv passes through UNWRAPPED and the
    entry refuses those platforms loudly at boot. TODO(P2): macOS variant;
    TODO(P3): Windows Job Objects.
    """
    if sys.platform != "linux":
        return list(argv)
    parent = os.getpid() if parent_pid is None else parent_pid
    starttime = read_proc_starttime(parent)
    return [
        sys.executable,
        "-m",
        "modulo.launcher.supervisor",
        "child-shim",
        "--parent-pid",
        str(parent),
        "--parent-starttime",
        str(starttime if starttime is not None else -1),
        "--",
        *argv,
    ]


def _set_pdeathsig() -> None:  # pragma: no cover - runs in the forked child
    """Pre-exec hook: PR_SET_PDEATHSIG (Linux) — kernel-side orphan guard.

    Belt-and-braces on top of the shim's STARTTIME watchdog: if the shim dies
    the kernel SIGKILLs the service immediately. Re-checks the parent after
    setting the signal to close the classic pre-exec race.
    """
    if sys.platform != "linux":
        return
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    pr_set_pdeathsig = 1
    libc.prctl(pr_set_pdeathsig, signal.SIGKILL)
    if os.getppid() == 1:
        os._exit(2)


def _shim_should_abandon(
    *,
    recorded_starttime: int,
    current_starttime: int | None,
) -> bool:
    """True when the launcher (shim's parent) is gone or was PID-reused."""
    if current_starttime is None:
        return True
    return current_starttime != recorded_starttime


def child_shim_main(argv: list[str]) -> int:
    """Run the child shim: supervise the launcher, carry the service process.

    Behaviour (ADR 031 Decision 5):

    * spawns the service with PDEATHSIG (Linux) and does NOT swallow group
      signals: the launcher signals the shim's process group, so both shim
      and service receive it; the shim stays alive just long enough to reap
      the service and propagates its exit code (a lone SIGTERM to the shim
      pid is forwarded to the service for the direct-kill edge);
    * polls the launcher's STARTTIME at most every 2 s; a missing or changed
      STARTTIME (parent death or PID reuse) kills the service and exits;
    * exits with the service's exit code so the supervisor's crash bookkeeping
      sees the real failure.
    """
    parser = argparse.ArgumentParser(prog="modulo-child-shim", add_help=False)
    parser.add_argument("child_shim")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-starttime", type=int, required=True)
    # argparse's REMAINDER positional mis-parses when optionals follow a
    # positional, so split the child argv off on the first "--" ourselves and
    # parse only the shim's own options.
    dash = argv.index("--") if "--" in argv else len(argv)
    child_argv = argv[dash + 1 :]
    ns = parser.parse_args(argv[:dash])
    if not child_argv:
        _log.error("shim.no_child_argv")
        return 2
    parent_pid = ns.parent_pid
    recorded = ns.parent_starttime
    shutting_down = threading.Event()
    child_holder: list[subprocess.Popen[bytes]] = []

    def _forward_signal(signum: int, _frame: object) -> None:
        # ONE handler for SIGTERM+SIGINT, installed BEFORE the child is
        # spawned: a signal arriving in the spawn window must not kill the
        # shim by default action. The group signal already reached the
        # service; a signal aimed at THIS pid only (not the group) is
        # forwarded on SIGTERM.
        shutting_down.set()
        if signum == signal.SIGTERM and child_holder:
            with contextlib.suppress(OSError):
                os.kill(child_holder[0].pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)
    preexec = _set_pdeathsig if sys.platform == "linux" else None
    child_proc = subprocess.Popen(  # noqa: S603 — argv built by the supervisor, never shell
        child_argv,
        preexec_fn=preexec,
    )
    child_holder.append(child_proc)

    next_watchdog = 0.0
    while True:
        code: int | None = child_proc.poll()
        if code is not None:
            return code
        now = time.monotonic()
        if now >= next_watchdog:
            next_watchdog = now + SHIM_POLL_SECONDS
            if _shim_should_abandon(
                recorded_starttime=recorded,
                current_starttime=read_proc_starttime(parent_pid),
            ):
                try:
                    child_proc.kill()
                    child_proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                return 2
        if shutting_down.is_set():
            try:
                return child_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child_proc.kill()
                return child_proc.wait(timeout=5)
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


@dataclass
class ChildSpec:
    """What the supervisor needs to own one child service."""

    name: str
    argv_builder: Callable[[], list[str]]
    # The probe answers True (healthy), False (unhealthy) or
    # ProbeOutcome.UNAVAILABLE (tool failure — never terminate on it).
    probe: Callable[[], bool | ProbeOutcome] | None = None
    start_condition: Callable[[], bool] | None = None
    env_builder: Callable[[dict[str, str]], dict[str, str]] | None = None
    shutdown_priority: int = 100
    # First teardown signal (SIGINT = PG fast shutdown); escalated to
    # SIGTERM then SIGKILL.
    teardown_signal: int = int(signal.SIGTERM)
    escalate_signal: int | None = int(signal.SIGTERM)
    # Optional longer first-phase wait (Postgres fast shutdown needs more
    # than the generic grace before escalating to smart/SIGTERM).
    shutdown_escalation_timeout: float | None = None


@dataclass
class _Child:
    spec: ChildSpec
    process: ChildProcess | None = None
    next_spawn_at: float | None = None
    backoff: float = 0.0
    healthy: bool = False
    spawned_at: float | None = None
    terminating_since: float | None = None
    probe_unavailable_logged: bool = False
    crash_times: deque[float] = field(default_factory=deque)


class Supervisor:
    """Owns the bundled-service + SAQ children of one launcher process.

    The monitor loop is a plain ``tick()`` so tests drive time explicitly
    (no wall-clock sleeps); the entry runs :meth:`monitor_loop` on a thread
    while uvicorn serves in the main thread.
    """

    def __init__(
        self,
        knobs: SupervisorKnobs,
        *,
        spawner: ChildSpawner | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        crash_hook: Callable[[str, int], None] | None = None,
        pause_hook: Callable[[str], None] | None = None,
        on_degraded: Callable[[str], None] | None = None,
        runtime_path: Path | None = None,
    ) -> None:
        self.knobs = knobs
        self._spawner: ChildSpawner = spawner if spawner is not None else _default_spawner
        self._clock = clock
        self._sleep = sleep
        self._crash_hook = crash_hook
        self._pause_hook = pause_hook
        self._on_degraded = on_degraded
        self._runtime_path = runtime_path
        self._children: dict[str, _Child] = {}
        self._mutex = threading.RLock()
        self._stop_event = threading.Event()
        self._degraded_reason: str | None = None
        self._monitor_thread: threading.Thread | None = None
        # Bundled postgres version, resolved once at boot (best effort) and
        # persisted into the runtime manifest so doctor's
        # `installed_bundle_pg_version` axis can detect a downgrade/upgrade
        # against the cluster's last-run version. None until `start()` runs
        # (or the binary is unresolvable).
        self._installed_bundle_pg_version: str | None = None

    # -- registration / lifecycle -------------------------------------------

    def add(self, spec: ChildSpec) -> None:
        with self._mutex:
            if spec.name in self._children:
                raise LauncherError(f"child {spec.name!r} is already registered")
            self._children[spec.name] = _Child(spec=spec, backoff=self.knobs.restart_backoff_initial)

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    def child_pids(self) -> dict[str, int]:
        with self._mutex:
            return {name: child.process.pid for name, child in self._children.items() if child.process is not None}

    def start(self, *, start_monitor: bool = True) -> None:
        """Spawn immediately-ready children and (optionally) the monitor thread."""
        self._installed_bundle_pg_version = _resolve_bundled_postgres_version()
        self.tick()
        if start_monitor:
            thread = threading.Thread(target=self.monitor_loop, name="modulo-supervisor", daemon=True)
            self._monitor_thread = thread
            thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()

    def monitor_thread_alive(self) -> bool:
        """True while the monitor thread is running (None = never started)."""
        return self._monitor_thread is not None and self._monitor_thread.is_alive()

    def monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # An unexpected raise must never kill the monitor silently:
                # degrade so the entry tears everything down and exits
                # nonzero instead of leaving orphaned children behind.
                _log.exception("supervisor.monitor_loop_failed")
                reason = f"supervisor monitor crashed: {sys.exc_info()[1]!r}"
                self._degrade_locked(reason)
                return
            if self._degraded_reason is not None:
                return
            self._sleep(self.knobs.tick_seconds)

    # -- monitor pass ---------------------------------------------------------

    def tick(self) -> None:
        """One supervision pass: deferred spawns, crash detection, backoff.

        Each child is guarded: ANY unexpected exception while supervising it
        (a race with a dying process, a spawn failure, a manifest write
        error) is logged and skipped — the monitor loop must survive to
        restart the remaining children.
        """
        if self._degraded_reason is not None:
            return
        with self._mutex:
            for child in self._children.values():
                try:
                    self._tick_child_locked(child)
                except Exception:
                    _log.exception("supervisor.tick_child_failed name=%s", child.spec.name)
                    continue

    def _tick_child_locked(self, child: _Child) -> None:
        now = self._clock()
        if child.process is None:
            if child.next_spawn_at is not None and now < child.next_spawn_at:
                return
            if self._ready_to_spawn(child):
                self._spawn_locked(child)
            return
        code = child.process.poll()
        if code is not None:
            self._on_exit_locked(child, code)
            return
        if child.spec.probe is None:
            # No probe: the startup deadline is the liveness contract — a
            # child that survives the window without dying is presumed live
            # (its death remains the crash signal).
            if not child.healthy and child.spawned_at is not None:
                self._enforce_startup_deadline_locked(child, now)
            return
        outcome = self._run_probe(child)
        if outcome is ProbeOutcome.UNAVAILABLE:
            # The probe TOOL is broken, not the child: leave the healthy
            # flag (and the child) untouched; log once per spell.
            if not child.probe_unavailable_logged:
                child.probe_unavailable_logged = True
                _log.warning("supervisor.probe_unavailable name=%s", child.spec.name)
            return
        if outcome is not ProbeOutcome.UNHEALTHY:
            child.healthy = True
            child.probe_unavailable_logged = False
            child.terminating_since = None
            child.backoff = self.knobs.restart_backoff_initial
            return
        child.probe_unavailable_logged = False
        if child.healthy:
            # A once-healthy child that now fails its probe is
            # restarted through the same crash/backoff path: stop it
            # here and let the exit poll record the real crash.
            if child.terminating_since is None:
                self._terminate_locked(child)
                child.terminating_since = now
            elif now - child.terminating_since > self.knobs.shutdown_grace_seconds:
                with contextlib.suppress(OSError):
                    child.process.kill()
            return
        self._enforce_startup_deadline_locked(child, now)

    def _enforce_startup_deadline_locked(self, child: _Child, now: float) -> None:
        """A never-healthy child must not wedge the supervisor forever.

        Past ``spawn_time + health_check_timeout``: probed children are
        terminated (the exit feeds the normal crash/cap path); children
        WITHOUT a probe (the SAQ workers) get the same deadline as a
        liveness contract — surviving the window without dying marks them
        healthy (their death remains the crash signal).
        """
        if child.spawned_at is None:
            return
        if now - child.spawned_at <= self.knobs.health_check_timeout:
            return
        if child.spec.probe is None:
            child.healthy = True
            _log.info("supervisor.child_presumed_live name=%s", child.spec.name)
            return
        process = child.process
        if process is None:
            return
        if child.terminating_since is None:
            _log.warning("supervisor.startup_deadline_missed name=%s", child.spec.name)
            self._terminate_locked(child)
            child.terminating_since = now
        elif now - child.terminating_since > self.knobs.shutdown_grace_seconds:
            with contextlib.suppress(OSError):
                process.kill()

    def _ready_to_spawn(self, child: _Child) -> bool:
        condition = child.spec.start_condition
        if condition is None:
            return True
        try:
            return bool(condition())
        except Exception:
            _log.exception("supervisor.start_condition_failed name=%s", child.spec.name)
            return False

    def _run_probe(self, child: _Child) -> ProbeOutcome:
        probe = child.spec.probe
        if probe is None:
            return ProbeOutcome.UNHEALTHY
        try:
            result = probe()
        except Exception:
            _log.exception("supervisor.probe_failed name=%s", child.spec.name)
            return ProbeOutcome.UNAVAILABLE
        if result is ProbeOutcome.UNAVAILABLE:
            return ProbeOutcome.UNAVAILABLE
        return ProbeOutcome.HEALTHY if result else ProbeOutcome.UNHEALTHY

    def _spawn_locked(self, child: _Child) -> None:
        argv = child.spec.argv_builder()
        env = child.spec.env_builder(dict(os.environ)) if child.spec.env_builder else None
        child.process = self._spawner(argv, env)
        child.next_spawn_at = None
        child.healthy = False
        child.spawned_at = self._clock()
        child.probe_unavailable_logged = False
        child.terminating_since = None
        self._record_runtime_locked()

    def _on_exit_locked(self, child: _Child, code: int) -> None:
        name = child.spec.name
        now = self._clock()
        child.process = None
        child.healthy = False
        child.spawned_at = None
        child.terminating_since = None
        child.probe_unavailable_logged = False
        if code == 0:
            # A clean exit is NOT a crash: it never feeds the cap or the
            # crash hook, and it clears the sliding window (a service that
            # ran to a clean exit proves its earlier crashes were
            # transient). The child is still respawned with the initial
            # backoff — the supervisor owns long-running services.
            child.crash_times.clear()
            child.backoff = self.knobs.restart_backoff_initial
            child.next_spawn_at = now + child.backoff
            _log.info("supervisor.child_clean_exit name=%s code=0", name)
            self._record_runtime_locked()
            return
        child.crash_times.append(now)
        window = self.knobs.crash_window_seconds
        while child.crash_times and now - child.crash_times[0] > window:
            child.crash_times.popleft()
        if self._crash_hook is not None:
            self._crash_hook(name, code)
        if len(child.crash_times) >= self.knobs.crash_cap:
            self._degrade_locked(
                f"child {name!r} crashed {len(child.crash_times)} times in the last "
                f"{window:.0f}s (cap {self.knobs.crash_cap})"
            )
            return
        if child.backoff <= 0:
            child.backoff = self.knobs.restart_backoff_initial
        child.next_spawn_at = now + child.backoff
        child.backoff = min(child.backoff * 2, self.knobs.restart_backoff_max)
        _log.warning("supervisor.child_restarting name=%s code=%s backoff=%.2f", name, code, child.backoff)
        self._record_runtime_locked()

    def _degrade_locked(self, reason: str) -> None:
        self._degraded_reason = reason
        _log.error("supervisor.crash_cap_tripped reason=%s", reason)
        self._record_runtime_locked()
        if self._pause_hook is not None:
            self._pause_hook(GATE_SUPERVISOR_PRE_TEARDOWN)
        else:
            _pause_at(GATE_SUPERVISOR_PRE_TEARDOWN)
        self.shutdown()
        if self._on_degraded is not None:
            self._on_degraded(reason)
        self._stop_event.set()

    # -- teardown -------------------------------------------------------------

    def _terminate_locked(self, child: _Child) -> None:
        process = child.process
        if process is None:
            return
        with contextlib.suppress(OSError):
            process.terminate()

    def shutdown(self) -> None:
        """Ordered teardown: SAQ -> Postgres (fast/smart) -> Redis. Idempotent."""
        with self._mutex:
            ordered = sorted(self._children.values(), key=lambda child: (child.spec.shutdown_priority, child.spec.name))
            for child in ordered:
                self._teardown_child_locked(child)
            self._stop_event.set()
            self._record_runtime_locked()

    @staticmethod
    def _wait_or_none(process: ChildProcess, timeout: float) -> int | None:
        """Reap the process within *timeout* (None = still running / failed)."""
        try:
            return process.wait(timeout=timeout)
        except Exception:
            return None

    def _teardown_child_locked(self, child: _Child) -> None:
        process = child.process
        if process is None:
            return
        grace = self.knobs.shutdown_grace_seconds
        self._signal_group(process, child.spec.teardown_signal)
        first_wait = grace
        if child.spec.shutdown_escalation_timeout is not None:
            first_wait = max(grace, child.spec.shutdown_escalation_timeout)
        if self._wait_or_none(process, first_wait) is not None:
            child.process = None
            return
        if child.spec.escalate_signal is not None:
            self._signal_group(process, child.spec.escalate_signal)
            if self._wait_or_none(process, grace) is not None:
                child.process = None
                return
        process.kill()
        if self._wait_or_none(process, grace) is None:
            _log.error("supervisor.teardown_kill_failed name=%s", child.spec.name)
        child.process = None

    @staticmethod
    def _signal_group(process: ChildProcess, signum: int) -> None:
        """Signal the child's whole process group (shim + service together)."""
        if sys.platform == "win32":
            # TODO(P3): Windows has no killpg; Job Objects own teardown then.
            with contextlib.suppress(OSError):
                process.terminate()
            return
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = -1
        if pgid == process.pid:
            # A dying process can lose its group between getpgid and killpg
            # — that race must never break the teardown of the remaining
            # children.
            with contextlib.suppress(OSError):
                os.killpg(pgid, signum)
        else:
            # The process is not the head of its own group (or we could not
            # resolve its group), so signal the single PID directly. Mirror
            # the contract the test doubles (and a real non-grouped child)
            # expose via process.terminate() as well, so the teardown is
            # observable regardless of whether the group signal went out.
            with contextlib.suppress(OSError):
                process.terminate()
            with contextlib.suppress(OSError):
                os.kill(process.pid, signum)

    def _record_runtime_locked(self) -> None:
        if self._runtime_path is None:
            return
        extra: dict[str, Any] = {}
        if self._degraded_reason is not None:
            extra["degraded_reason"] = self._degraded_reason
        if self._installed_bundle_pg_version is not None:
            extra["installed_bundle_pg_version"] = self._installed_bundle_pg_version
        try:
            write_runtime_manifest(self._runtime_path, self.child_pids(), extra=extra or None)
        except OSError:
            _log.exception("supervisor.runtime_manifest_write_failed path=%s", self._runtime_path)


# ---------------------------------------------------------------------------
# Read-only helpers for doctor/status (FAR-676): log paths, log tails,
# size-based rotation, degraded reason. NEVER mutate data-dir state.
# ---------------------------------------------------------------------------


def log_paths(data_dir: Path) -> dict[str, Path]:
    """Map log component name -> path inside the data dir (no files created).

    ``app`` is the launcher/supervisor log (launcher.log); ``postgres`` and
    ``redis`` are the bundled children's logs under ``logs/``.
    """
    return {
        APP_LOG_NAME: data_dir / LAUNCHER_LOG_FILENAME,
        "postgres": data_dir / LOGS_DIRNAME / "postgres.log",
        "redis": data_dir / LOGS_DIRNAME / "redis.log",
    }


def read_log_tail(path: Path, *, max_bytes: int = DEFAULT_LOG_TAIL_BYTES) -> str:
    """Return at most *max_bytes* of the tail of *path* (decode-tolerant)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def rotate_log(
    path: Path,
    *,
    max_bytes: int | None = None,
    keep: int | None = None,
) -> bool:
    """Shift-size rotation: rotate *path* to ``<name>.1`` when over *max_bytes*.

    Defaults to the module constants (resolved at CALL time so the operator
    seam stays monkeypatchable). At most *keep* retained numeric generations
    (``.1`` .. ``.keep``), the oldest is dropped. Returns True when a
    rotation happened; False when the file is absent or below the threshold.
    Safe ONLY while no process holds the file open for appending (an
    attached fd keeps writing after the rename); callers must guarantee
    that (e.g. the launcher is stopped).
    """
    effective_max = max_bytes if max_bytes is not None else ROTATE_DEFAULT_MAX_BYTES
    effective_keep = keep if keep is not None else ROTATE_DEFAULT_KEEP
    if effective_keep < 1:
        raise ValueError("keep must be >= 1")
    try:
        if not path.is_file() or path.stat().st_size < effective_max:
            return False
        oldest = path.with_name(f"{path.name}.{effective_keep}")
        if oldest.exists():
            oldest.unlink()
        for generation in range(effective_keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{generation}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{generation + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
        return True
    except OSError:
        _log.warning("supervisor.log_rotation_failed path=%s", path)
        return False


def _resolve_bundled_postgres_version() -> str | None:
    """Best-effort bundled postgres version (``postgres --version`` output).

    Used at supervisor boot to persist ``installed_bundle_pg_version`` into the
    runtime manifest so doctor can detect a bundled-binary downgrade/upgrade
    against the cluster's last-run version. Returns None when the binary is
    missing or unresolvable (the doctor axis then honestly skips).
    """
    from modulo.launcher.entry import resolve_bin_dir

    bin_dir = resolve_bin_dir()
    binary = bin_dir / ("postgres.exe" if sys.platform == "win32" else "postgres")
    if not binary.is_file():
        return None
    try:
        result = subprocess.run(  # noqa: S603 — argv fully pinned
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for token in result.stdout.split():
        if token and token[0].isdigit() and "." in token:
            return token
    return None


def _default_spawner(argv: list[str], env: dict[str, str] | None) -> ChildProcess:
    """Spawn a shim-wrapped child in its own process group (POSIX)."""
    shim_argv = child_shim_argv(argv)
    return subprocess.Popen(  # noqa: S603 — argv fully constructed by the supervisor
        shim_argv,
        env=env,
        start_new_session=sys.platform != "win32",
    )


def _pause_at(gate: str) -> None:
    """Deterministic pre-teardown pause for tests (see module docstring)."""
    if os.environ.get(_PAUSE_ENV_VAR) != gate:
        return
    sys.stderr.write(f"PAUSED:{gate}\n")
    sys.stderr.flush()
    sys.stdin.readline()


# ---------------------------------------------------------------------------
# Boot-time orphan reconciliation
# ---------------------------------------------------------------------------


def _pid_starttime_epoch(pid: int) -> float | None:
    """Approximate the epoch time a /proc process started (Linux only)."""
    if sys.platform != "linux":
        return None
    ticks = read_proc_starttime(pid)
    if ticks is None:
        return None
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return None
    if hz <= 0:
        return None
    return time.time() - uptime + ticks / hz


def reconcile_orphans(pgdata: Path, *, tolerance_seconds: float = 5.0) -> str | None:
    """Validate/remove a stale ``postmaster.pid`` and sweep SIGKILL debris.

    Returns a short description of what was done (for logs); raises
    :class:`LauncherError` when a LIVE postgres still holds the pidfile —
    that instance must be stopped by its owner, never raced.
    """
    pidfile = pgdata / POSTMASTER_PIDFILE
    action: str | None = None
    if pidfile.exists():
        stale = _postmaster_is_stale(pidfile, tolerance_seconds=tolerance_seconds)
        if stale is not True:
            raise LauncherError(
                f"Refusing to start: {pidfile} belongs to a live postgres process. "
                "Stop that postgres first; the launcher never races another postmaster."
            )
        pidfile.unlink(missing_ok=True)
        action = "removed_stale_postmaster_pid"
        _log.warning("supervisor.stale_postmaster_pid_removed path=%s", pidfile)
    if pgdata.parent.exists():
        _sweep_stale_tmp_dirs(pgdata.parent)
        _sweep_stale_redis_confs(pgdata.parent)
        action = action or "swept_debris"
    return action


def _sweep_stale_redis_confs(parent: Path) -> None:
    """Remove per-boot Redis config files left by a killed previous attempt.

    Each boot writes a fresh ``.redis-conf-<random>`` (0600, requirepass
    inside) next to pgdata; the SIGKILL-debris sweep treats them like the
    initdb pwfiles — a surviving file carries the Redis password and must
    never accumulate.
    """
    for entry in parent.glob(f"{REDIS_CONF_PREFIX}*"):
        if entry.is_file():
            with contextlib.suppress(OSError):
                entry.unlink()


def _postmaster_is_stale(pidfile: Path, *, tolerance_seconds: float) -> bool | None:
    """True = stale (safe to remove); False/None = live postgres (refuse).

    A missing/unparseable pidfile counts as stale debris. A live PID is
    provably not postgres when its STARTTIME no longer matches the recorded
    start (PID reuse) or its /proc identity is not a postgres process.
    """
    try:
        lines = pidfile.read_text(encoding="ascii").splitlines()
        recorded_pid = int(lines[0].strip())
        recorded_start = float(lines[2].strip())
    except (OSError, ValueError, IndexError):
        return True
    if not _pid_alive(recorded_pid):
        return True
    actual_start = _pid_starttime_epoch(recorded_pid)
    if actual_start is not None and abs(actual_start - recorded_start) > tolerance_seconds:
        return True  # PID reused by a different process
    is_postgres = _is_postgres_process(recorded_pid)
    if is_postgres is None:
        return None  # identity unknowable (no access) — never race a live postgres
    return not is_postgres


def _is_postgres_process(pid: int) -> bool | None:
    """True/False when /proc/<pid>/cmdline is readable; None when unknowable.

    Off Linux the identity is UNKNOWABLE (no /proc) — returning False here
    would let reconciliation delete a live foreign postgres's pidfile, so
    the only safe answer is None (refuse).
    """
    if sys.platform != "linux":
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
    except PermissionError:
        return None
    except OSError:
        return False
    return any(part.endswith(b"postgres") for part in cmdline if part)


# ---------------------------------------------------------------------------
# Runtime manifest (child PIDs) — credential-free bookkeeping
# ---------------------------------------------------------------------------


def write_runtime_manifest(path: Path, pids: dict[str, int], *, extra: dict[str, Any] | None = None) -> None:
    """Persist supervisor child PIDs next to state.json (atomic, 0o600).

    state.json's v1 payload is frozen (slice-1 HMAC contract + its tests),
    so the child PIDs live in this sibling manifest until a schema-2 bump
    can fold them in. Credential-free by construction. ``extra`` lets the
    supervisor persist one-off bookkeeping beside the PIDs (the degraded
    reason when it degrades); extra values must themselves be
    credential-free and JSON-serialisable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"children": pids}
    if extra:
        payload["extra"] = extra
    tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp_path.replace(path)


def read_runtime_manifest(path: Path) -> dict[str, int]:
    """Read the child-PID manifest (missing/corrupt = no children known)."""
    children = _read_manifest_fields(path).get("children", {})
    if not isinstance(children, dict):
        return {}
    return {name: pid for name, pid in children.items() if isinstance(pid, int)}


def read_degraded_reason(path: Path) -> str | None:
    """Read the persisted degraded reason (None = not degraded / no manifest)."""
    extra = _read_manifest_fields(path).get("extra")
    if not isinstance(extra, dict):
        return None
    reason = extra.get("degraded_reason")
    return reason if isinstance(reason, str) else None


def _read_manifest_fields(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _pid_alive(pid: int) -> bool:
    """Is *pid* running? POSIX uses kill(0); Windows uses OpenProcess.

    Windows must NOT use ``os.kill(pid, 0)`` as a liveness probe: it opens a
    SYNCHRONIZE|PROCESS_TERMINATE handle and (empirically, Python 3.12 on
    Windows) leaves subsequent ``WaitForSingleObject`` waits on OTHER
    process handles hanging forever — which wedged every later
    ``subprocess.Popen().wait()`` in the test session.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        kernel32: Any = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return int(kernel32.WaitForSingleObject(handle, 0)) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def collect_status(data_dir: Path) -> dict[str, Any]:
    """Assemble the ``modulo status`` payload (state + pids + lock holder).

    Strictly READ-ONLY — a status query must never mutate the data dir: the
    secrets file is parsed WITHOUT the create-if-missing behaviour (a
    missing file simply means ``initialized: false``), nothing is created,
    chmodded, or rewritten. state.json is verified with the secrets-file
    HMAC key when readable, and the result carries only ports/PIDs/modes.
    """
    from modulo.launcher import secrets_file as secrets_file_module
    from modulo.launcher.secrets_file import SecretsFileError
    from modulo.launcher.state import LauncherState, StateIntegrityError, StateVersionError, load_state

    status: dict[str, Any] = {"data_dir": str(data_dir), "initialized": False, "components": {}}
    state: LauncherState | None = None
    secrets_path = data_dir / "secrets.json"
    try:
        if not secrets_path.exists():
            status["error"] = "data dir is not initialized (no secrets file)"
        else:
            secrets_loaded = secrets_file_module._parse(secrets_path.read_bytes())
            state = load_state(data_dir / STATE_FILENAME, secrets_loaded.state_hmac_key)
    except SecretsFileError as exc:
        status["error"] = f"secrets unavailable: {exc}"
    except (StateIntegrityError, StateVersionError) as exc:
        status["error"] = f"state unreadable: {exc}"
    except FileNotFoundError:
        pass
    if state is None:
        return status
    status["initialized"] = True
    status["postgres_port"] = state.postgres_port
    status["redis_port"] = state.redis_port
    status["api_port"] = state.api_port
    runtime_path = data_dir / RUNTIME_FILENAME
    holder = _read_lock_holder(data_dir.parent / (data_dir.name + LOCK_SUFFIX))
    pids = read_runtime_manifest(runtime_path)
    degraded_reason = read_degraded_reason(runtime_path)
    if holder is not None:
        status["launcher"] = {
            "pid": holder.pid,
            "mode": holder.mode,
            "alive": _pid_alive(holder.pid),
        }
    if degraded_reason is not None:
        status["degraded"] = {
            "reason": degraded_reason,
            "remediation": (
                "the supervisor trip is terminal — restart `modulo start` once the underlying "
                "fault is cleared, and inspect the app log (`modulo logs`) for crash backtraces"
            ),
        }
    for name, port in (
        ("postgres", state.postgres_port),
        ("redis", state.redis_port),
        ("saq-runs", None),
        ("saq-system", None),
    ):
        pid = pids.get(name)
        status["components"][name] = {
            "pid": pid,
            "alive": _pid_alive(pid) if pid is not None else False,
            "port": port,
            "state": _component_state(pid, port),
            "remediation": _component_remediation(name, pid, port),
        }
    api_pid = holder.pid if holder is not None else None
    status["components"]["api"] = {
        "pid": api_pid,
        "alive": bool(api_pid is not None and _pid_alive(api_pid)),
        "port": state.api_port,
        "state": _component_state(api_pid, state.api_port),
        "remediation": _component_remediation("api", api_pid, state.api_port),
    }
    return status


def _component_state(pid: int | None, port: int | None) -> str:
    """One-word per-component state (``modulo status --json`` enrichment)."""
    if pid is not None and _pid_alive(pid):
        return "healthy"
    return "stopped"


def _component_remediation(name: str, pid: int | None, port: int | None) -> str | None:
    """A short operator hint when the component is NOT healthy."""
    if pid is not None and _pid_alive(pid):
        return None
    if name == "postgres":
        return f"postgres is not running on 127.0.0.1:{port} — restart `modulo start`"
    if name == "redis":
        return f"redis is not running on 127.0.0.1:{port} — restart `modulo start`"
    if name == "api":
        return "the api process owns the data-dir lock while serving; restart `modulo start`"
    return f"{name} worker is not running — restart `modulo start`"


def _shim_cli_entry(argv: list[str]) -> int:  # pragma: no cover - __main__ only
    """Dispatch ``python -m modulo.launcher.supervisor child-shim ...``."""
    if argv and argv[0] == "child-shim":
        return child_shim_main(argv)
    _log.error("shim.unknown_subcommand argv=%r", argv)
    return 2


if __name__ == "__main__":  # pragma: no cover - the child-shim entrypoint
    sys.exit(_shim_cli_entry(sys.argv[1:]))
