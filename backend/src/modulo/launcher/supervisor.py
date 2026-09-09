"""In-app supervisor + data-dir lock (ADR 031 Decisions 5).

The supervisor owns the bundled Postgres/Redis AND the SAQ worker children —
all as child processes of the launcher process. The OS service manager (P1b)
owns only the launcher's own restart, never a child's.

Contracts locked by tests (``tests/unit/launcher/test_supervisor.py``):

* **Exclusive data-dir lock**: the launcher holds an OS-advisory
  ``flock`` on the ``<datadir>.lock`` sibling file for its WHOLE lifetime.
  The kernel releases it on process death, so a SIGKILLed launcher can never
  wedge the data dir. A concurrent start refuses and names the holder's
  PID + mode. POSIX-only: Windows carries the TODO(P3) Job-Object seam and
  refuses loudly.
* **Restart backoff + sliding-window crash cap**: a crashed/unhealthy child
  is restarted with exponential backoff (initial → max); crash timestamps
  land in a sliding window and exceeding the cap (default 5 failures per
  10 minutes) trips the terminal degraded state: ordered teardown, nonzero
  exit, and the doctor/repair hint. Counters arm at spawn; deferred children
  (SAQ behind migration-head readiness) cannot crash before their start
  condition is met because they are not spawned until it is.
* **Child shim owns death**: every supervised child runs under a shim
  process (``python -m modulo.launcher.supervisor child-shim``) that polls
  the launcher's ``/proc/<ppid>/stat`` STARTTIME (field 22) at most every
  2 seconds — PID-reuse safe — and kills the child when the parent is gone.
  On Linux the child additionally carries PDEATHSIG (pre-exec) as a
  belt-and-braces optimisation. TODO(P3): Windows Job Objects.
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
import json
import logging
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
__all__ = [
    "GATE_SUPERVISOR_PRE_TEARDOWN",
    "RUNTIME_FILENAME",
    "ChildSpec",
    "DataDirLock",
    "DataDirLockError",
    "LauncherError",
    "Supervisor",
    "SupervisorKnobs",
    "_children_of",
    "child_shim_argv",
    "child_shim_main",
    "collect_status",
    "knobs_from_env",
    "read_proc_starttime",
    "read_runtime_manifest",
    "reconcile_orphans",
    "request_stop",
    "write_runtime_manifest",
]


class LauncherError(RuntimeError):
    """Base class for launcher failures surfaced to the CLI."""


class DataDirLockError(LauncherError):
    """Raised when another launcher holds the exclusive data-dir lock."""


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


def knobs_from_env(env: dict[str, str] | None = None) -> SupervisorKnobs:
    """Build knobs from ``MODULO_LAUNCHER_*`` env overrides (operator seam).

    Until Settings grows launcher fields, the launcher honours these env
    overrides; unknown/invalid values fall back to the production default.
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
        if value <= 0:
            _log.warning("supervisor.knob_ignored_nonpositive name=%s value=%r", var, raw)
            continue
        default = getattr(defaults, attr)
        overrides[attr] = float(value) if isinstance(default, float) else int(value)
    return SupervisorKnobs(**overrides)


# ---------------------------------------------------------------------------
# Data-dir lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockHolder:
    """The launcher identity recorded inside the lock file after acquiring."""

    pid: int
    mode: str
    acquired_at: float


def _read_lock_holder(path: Path) -> LockHolder | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = payload.get("pid")
    mode = payload.get("mode")
    acquired_at = payload.get("acquired_at")
    if not isinstance(pid, int) or not isinstance(mode, str):
        return None
    return LockHolder(pid=pid, mode=mode, acquired_at=acquired_at if isinstance(acquired_at, float) else 0.0)


class DataDirLock:
    """Exclusive OS-advisory lock over one data dir (kernel-released).

    The lock file is the ``<datadir>.lock`` SIBLING of the data dir (outside
    it, so a data-dir reset cannot hide a live lock). Acquisition is
    ``flock(LOCK_EX | LOCK_NB)``; the kernel releases it when the holding
    process dies, which is exactly the semantics ADR 031 Decision 5 wants.
    The holder's PID + mode are written into the file after acquiring so a
    refused concurrent start can name the culprit.
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
            if holder is not None and holder.pid != os.getpid():
                raise DataDirLockError(
                    f"Refusing to start: data dir {self.data_dir} is locked by another launcher "
                    f"(holder PID {holder.pid}, mode {holder.mode!r}). Stop that launcher first "
                    "(or remove the stale lock file only if the holder process is confirmed dead)."
                ) from exc
            raise DataDirLockError(
                f"Refusing to start: data dir {self.data_dir} is locked by another process "
                f"({exc}). Another launcher may be mid-boot."
            ) from exc
        self._fd = fd
        holder = LockHolder(pid=os.getpid(), mode=self.mode, acquired_at=time.time())
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(
                fd,
                json.dumps({"pid": holder.pid, "mode": holder.mode, "acquired_at": holder.acquired_at}).encode(),
            )
            os.fsync(fd)
        except OSError:
            self.release()
            raise

    def release(self) -> None:
        """Release the flock (idempotent; the file itself is never unlinked)."""
        if self._fd is None:
            return
        if sys.platform != "win32":
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                _log.warning("supervisor.lock_unlock_failed path=%s", self._path)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def request_stop(data_dir: Path, *, timeout: float = 10.0) -> int:
    """Ask the launcher holding *data_dir* to stop (the ``modulo stop`` path).

    Sends SIGTERM to the lock holder and waits for the lock to clear.
    POSIX-only; returns 0 when the launcher stopped, 1 when it could not be
    confirmed stopped.
    """
    if sys.platform == "win32":
        # TODO(P3): Windows service-control seam.
        raise LauncherError("modulo stop is not supported on Windows yet (TODO(P3))")
    lock_path = data_dir.parent / (data_dir.name + LOCK_SUFFIX)
    holder = _read_lock_holder(lock_path)
    if holder is None:
        _log.info("stop.no_holder data_dir=%s", data_dir)
        return 0
    try:
        os.kill(holder.pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    except OSError as exc:
        raise LauncherError(f"could not signal launcher PID {holder.pid}: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_lock_holder(lock_path) is None:
            return 0
        try:
            os.kill(holder.pid, 0)
        except OSError:
            return 0
        time.sleep(0.2)
    raise LauncherError(
        f"launcher PID {holder.pid} did not stop within {timeout}s — check the launcher log in the data dir"
    )


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
    launcher dies — PID-reuse safe via the STARTTIME comparison. On
    platforms without ``/proc`` (Windows/macOS) the shim is unavailable and
    the argv passes through unwrapped: TODO(P2)/TODO(P3) replace with the
    platform-native death-binding mechanism.
    """
    if sys.platform == "win32":
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
    parser.add_argument("child_argv", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    child_argv = ns.child_argv
    if child_argv and child_argv[0] == "--":
        child_argv = child_argv[1:]
    if not child_argv:
        _log.error("shim.no_child_argv")
        return 2
    parent_pid = ns.parent_pid
    recorded = ns.parent_starttime
    shutting_down = threading.Event()

    def _mark_shutting_down(_signum: int, _frame: object) -> None:
        # The group signal already reached the service; just keep reaping.
        shutting_down.set()

    signal.signal(signal.SIGTERM, _mark_shutting_down)
    signal.signal(signal.SIGINT, _mark_shutting_down)
    preexec = _set_pdeathsig if sys.platform == "linux" else None
    child_proc = subprocess.Popen(  # noqa: S603 — argv built by the supervisor, never shell
        child_argv,
        preexec_fn=preexec,
    )

    def _forward_terminate(signum: int, _frame: object) -> None:
        # Signal aimed at THIS pid only (not the group): forward it.
        shutting_down.set()
        if signum == signal.SIGTERM:
            with contextlib.suppress(OSError):
                os.kill(child_proc.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, _forward_terminate)
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
    probe: Callable[[], bool] | None = None
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
    terminating_since: float | None = None
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
        self.tick()
        if start_monitor:
            thread = threading.Thread(target=self.monitor_loop, name="modulo-supervisor", daemon=True)
            thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()

    def monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            self.tick()
            if self._degraded_reason is not None:
                return
            self._sleep(self.knobs.tick_seconds)

    # -- monitor pass ---------------------------------------------------------

    def tick(self) -> None:
        """One supervision pass: deferred spawns, crash detection, backoff."""
        if self._degraded_reason is not None:
            return
        with self._mutex:
            for child in self._children.values():
                now = self._clock()
                if child.process is None:
                    if child.next_spawn_at is not None and now < child.next_spawn_at:
                        continue
                    if self._ready_to_spawn(child):
                        self._spawn_locked(child)
                    continue
                code = child.process.poll()
                if code is not None:
                    self._on_exit_locked(child, code)
                    continue
                if child.spec.probe is None:
                    continue
                ok = self._run_probe(child)
                if ok:
                    child.healthy = True
                    child.terminating_since = None
                    child.backoff = self.knobs.restart_backoff_initial
                elif child.healthy:
                    # A once-healthy child that now fails its probe is
                    # restarted through the same crash/backoff path: stop it
                    # here and let the exit poll record the real crash.
                    if child.terminating_since is None:
                        self._terminate_locked(child)
                        child.terminating_since = now
                    elif now - child.terminating_since > self.knobs.shutdown_grace_seconds:
                        with contextlib.suppress(OSError):
                            child.process.kill()

    def _ready_to_spawn(self, child: _Child) -> bool:
        condition = child.spec.start_condition
        if condition is None:
            return True
        try:
            return bool(condition())
        except Exception:
            _log.exception("supervisor.start_condition_failed name=%s", child.spec.name)
            return False

    def _run_probe(self, child: _Child) -> bool:
        probe = child.spec.probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:
            _log.exception("supervisor.probe_failed name=%s", child.spec.name)
            return False

    def _spawn_locked(self, child: _Child) -> None:
        argv = child.spec.argv_builder()
        env = child.spec.env_builder(dict(os.environ)) if child.spec.env_builder else None
        child.process = self._spawner(argv, env)
        child.next_spawn_at = None
        child.healthy = False
        child.terminating_since = None
        self._record_runtime_locked()

    def _on_exit_locked(self, child: _Child, code: int) -> None:
        name = child.spec.name
        now = self._clock()
        child.crash_times.append(now)
        window = self.knobs.crash_window_seconds
        while child.crash_times and now - child.crash_times[0] > window:
            child.crash_times.popleft()
        child.process = None
        child.healthy = False
        child.terminating_since = None
        if self._crash_hook is not None:
            self._crash_hook(name, code)
        if len(child.crash_times) > self.knobs.crash_cap:
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
            os.killpg(pgid, signum)
        else:
            with contextlib.suppress(OSError):
                os.kill(process.pid, signum)

    def _record_runtime_locked(self) -> None:
        if self._runtime_path is None:
            return
        write_runtime_manifest(self._runtime_path, self.child_pids())


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
        action = action or "swept_debris"
    return action


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
    """True/False when /proc/<pid>/cmdline is readable; None when unknowable."""
    if sys.platform != "linux":
        return False
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


def write_runtime_manifest(path: Path, pids: dict[str, int]) -> None:
    """Persist supervisor child PIDs next to state.json (atomic, 0o600).

    state.json's v1 payload is frozen (slice-1 HMAC contract + its tests),
    so the child PIDs live in this sibling manifest until a schema-2 bump
    can fold them in. Credential-free by construction.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({"children": pids}, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp_path.replace(path)


def read_runtime_manifest(path: Path) -> dict[str, int]:
    """Read the child-PID manifest (missing/corrupt = no children known)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    children = payload.get("children") if isinstance(payload, dict) else None
    if not isinstance(children, dict):
        return {}
    return {name: pid for name, pid in children.items() if isinstance(pid, int)}


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

    Never touches credentials: state.json is verified with the secrets-file
    HMAC key when readable, and the result carries only ports/PIDs/modes.
    """
    from modulo.launcher.secrets_file import SecretsFileError
    from modulo.launcher.state import LauncherState, StateIntegrityError, StateVersionError, load_state

    status: dict[str, Any] = {"data_dir": str(data_dir), "initialized": False, "components": {}}
    state: LauncherState | None = None
    try:
        from modulo.launcher.secrets_file import load_or_create

        secrets_loaded = load_or_create(data_dir / "secrets.json")
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
    holder = _read_lock_holder(data_dir.parent / (data_dir.name + LOCK_SUFFIX))
    pids = read_runtime_manifest(data_dir / RUNTIME_FILENAME)
    if holder is not None:
        status["launcher"] = {
            "pid": holder.pid,
            "mode": holder.mode,
            "alive": _pid_alive(holder.pid),
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
        }
    status["components"]["api"] = {
        "pid": holder.pid if holder is not None else None,
        "alive": bool(holder is not None and _pid_alive(holder.pid)),
        "port": state.api_port,
    }
    return status


def _shim_cli_entry(argv: list[str]) -> int:  # pragma: no cover - __main__ only
    """Dispatch ``python -m modulo.launcher.supervisor child-shim ...``."""
    if argv and argv[0] == "child-shim":
        return child_shim_main(argv)
    _log.error("shim.unknown_subcommand argv=%r", argv)
    return 2


if __name__ == "__main__":  # pragma: no cover - the child-shim entrypoint
    sys.exit(_shim_cli_entry(sys.argv[1:]))
