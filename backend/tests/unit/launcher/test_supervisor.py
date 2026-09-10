"""Unit tests for the in-app supervisor + data-dir lock (FAR-671 slice 2).

Locks: exclusive-lock refusal naming the holder, kernel-released locks,
crash-cap → degraded → teardown ordering, exponential backoff with sliding
window eviction, deferred (migration-head-gated) SAQ spawns, probe-failure
restarts, real-process orphan-free teardown, orphan reconciliation, the
runtime manifest, and the collect_status payload.

Timing tests use manual ``tick()`` driving with a fake clock — no
wall-clock sleeps; real-process tests use handshakes and bounded constant
polls, never computed sleeps (repo timing lesson).
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import modulo.launcher.secrets_file as secrets_file_module
from modulo.launcher import policy as policy_module
from modulo.launcher.secrets_file import load_or_create
from modulo.launcher.state import LauncherState, save_state
from modulo.launcher.supervisor import (
    ChildSpec,
    DataDirLock,
    DataDirLockError,
    LauncherError,
    ProbeOutcome,
    Supervisor,
    SupervisorKnobs,
    _children_of,
    _is_postgres_process,
    _shim_should_abandon,
    child_shim_argv,
    collect_status,
    knobs_from_env,
    read_proc_starttime,
    read_runtime_manifest,
    reconcile_orphans,
    request_stop,
    write_runtime_manifest,
)

POSIX = sys.platform != "win32"
requires_posix = pytest.mark.skipif(not POSIX, reason="POSIX-only supervision mechanics")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class FakeProc:
    """Duck-typed process that has already exited (or stays alive on demand)."""

    _next_pid = 424242

    def __init__(self, *, exit_code: int = 7, alive: bool = False) -> None:
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.exit_code = exit_code
        self.alive = alive
        self.polls = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        self.polls += 1
        if self.alive:
            return None
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.alive else self.exit_code


class RecordingProc:
    """Duck-typed process recording teardown interactions in order."""

    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.pid = 5000 + len(events)

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.events.append(f"term:{self.name}")

    def kill(self) -> None:
        self.events.append(f"kill:{self.name}")

    def wait(self, timeout: float | None = None) -> int:
        self.events.append(f"wait:{self.name}")
        return 0


class AliveProc:
    """Always-alive duck-typed process recording terminate/kill (deadline paths)."""

    _next_pid = 61000

    def __init__(self) -> None:
        AliveProc._next_pid += 1
        self.pid = AliveProc._next_pid
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class Harness:
    """Manual-clock supervisor with recorded spawns/hooks (no threads)."""

    def __init__(
        self,
        knobs: SupervisorKnobs | None = None,
        spec: ChildSpec | None = None,
        *,
        degraded_path: Path | None = None,
        degraded_context: Callable[[], dict[str, object]] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self.clock = FakeClock()
        self.spawned_argv: list[list[str]] = []
        self.processes: list[FakeProc] = []
        self.crashes: list[tuple[str, int]] = []
        self.degraded: list[str] = []
        if knobs is None:
            knobs = SupervisorKnobs(tick_seconds=0.01, restart_backoff_initial=0.01, restart_backoff_max=0.05)
        self.supervisor = Supervisor(
            knobs,
            spawner=self._spawn,
            clock=self.clock,
            sleep=lambda _seconds: None,
            crash_hook=self._crash,
            on_degraded=self._degraded,
            degraded_path=degraded_path,
            degraded_context=degraded_context,
            wall_clock=wall_clock if wall_clock is not None else (lambda: 1720000000.0),
        )
        self.supervisor.add(
            spec if spec is not None else ChildSpec(name="postgres", argv_builder=lambda: ["postgres-child"])
        )

    def _spawn(self, argv: list[str], env: dict[str, str] | None) -> FakeProc:
        self.spawned_argv.append(argv)
        proc = FakeProc()
        self.processes.append(proc)
        return proc

    def _crash(self, name: str, code: int) -> None:
        self.crashes.append((name, code))

    def _degraded(self, reason: str) -> None:
        self.degraded.append(reason)


def test_crash_cap_trips_degraded_stops_spawning_and_records_reason() -> None:
    harness = Harness(
        knobs=SupervisorKnobs(
            restart_backoff_initial=0.01,
            restart_backoff_max=0.02,
            crash_window_seconds=2.0,
            crash_cap=3,
        )
    )
    # Spawn, then three crash cycles — the cap trips ON the Nth crash (>=).
    harness.supervisor.tick()  # spawn #1
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # crash 1 → respawn scheduled at +0.01
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # spawn #2
    harness.supervisor.tick()  # crash 2
    harness.clock.advance(0.03)
    harness.supervisor.tick()  # spawn #3
    harness.supervisor.tick()  # crash 3 (== cap → trips NOW)
    assert harness.supervisor.degraded_reason is not None
    assert "postgres" in harness.supervisor.degraded_reason
    assert len(harness.degraded) == 1
    assert len(harness.crashes) == 3
    spawns_after_degrade = len(harness.spawned_argv)
    harness.clock.advance(1.0)
    harness.supervisor.tick()
    assert len(harness.spawned_argv) == spawns_after_degrade


def test_clean_exit_is_not_a_crash_and_clears_the_window() -> None:
    """Exit code 0 never feeds the cap or the crash hook, and resets the window."""
    harness = Harness(
        knobs=SupervisorKnobs(
            restart_backoff_initial=0.01,
            restart_backoff_max=0.02,
            crash_window_seconds=10.0,
            crash_cap=2,
        )
    )

    class ExitCodeProc(FakeProc):
        def __init__(self, code: int) -> None:
            super().__init__(exit_code=code)

    codes: list[int] = [7, 0, 7]
    harness.supervisor._spawner = lambda argv, env: ExitCodeProc(codes.pop(0) if codes else 7)  # type: ignore[method-assign]
    harness.supervisor.tick()  # spawn #1 (will exit 7)
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # crash 1 recorded → respawn at +0.01, backoff 0.02
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # spawn #2 (will exit 0)
    harness.supervisor.tick()  # clean exit: window cleared, NOT a crash
    assert harness.crashes == [("postgres", 7)]
    child = harness.supervisor._children["postgres"]
    assert not child.crash_times
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # spawn #3 (exits 7 — the clean exit reset the window)
    harness.supervisor.tick()  # crash with 7 — only ONE crash in the window now
    assert harness.supervisor.degraded_reason is None


def test_backoff_resets_to_initial_after_a_clean_exit() -> None:
    harness = Harness(
        knobs=SupervisorKnobs(
            restart_backoff_initial=0.01,
            restart_backoff_max=0.02,
            crash_window_seconds=1000.0,
            crash_cap=100,
        )
    )

    class ToggleProc(FakeProc):
        def __init__(self, code: int) -> None:
            super().__init__(exit_code=code)

    codes: list[int] = [7, 0]
    harness.supervisor._spawner = lambda argv, env: ToggleProc(codes.pop(0) if codes else 7)  # type: ignore[method-assign]
    harness.supervisor.tick()  # spawn #1 → crash 7
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # crash 1 → backoff doubled to 0.02
    child = harness.supervisor._children["postgres"]
    assert child.backoff == pytest.approx(0.02)
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # spawn #2 → exits 0
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # clean exit → backoff reset
    assert child.backoff == pytest.approx(0.01)


def test_backoff_progression_is_exponential_and_capped() -> None:
    harness = Harness(
        knobs=SupervisorKnobs(
            restart_backoff_initial=0.01,
            restart_backoff_max=0.05,
            crash_window_seconds=1000.0,
            crash_cap=100,
        )
    )
    harness.supervisor.tick()  # spawn #1
    child = harness.supervisor._children["postgres"]
    for expected_gap in (0.01, 0.02, 0.04, 0.05, 0.05):
        harness.supervisor.tick()  # detect the exit → schedule the respawn
        assert child.next_spawn_at is not None
        assert child.next_spawn_at - harness.clock.now == pytest.approx(expected_gap)
        harness.clock.advance(expected_gap)
        harness.supervisor.tick()  # the backoff elapsed → respawn
        assert child.next_spawn_at is None


def test_sliding_window_eviction_prevents_cap_trip() -> None:
    harness = Harness(
        knobs=SupervisorKnobs(
            restart_backoff_initial=0.01,
            restart_backoff_max=0.02,
            crash_window_seconds=1.0,
            crash_cap=3,
        )
    )
    harness.supervisor.tick()  # spawn #1
    harness.supervisor.tick()  # crash 1
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # spawn #2
    harness.supervisor.tick()  # crash 2 — below the cap (3), no trip
    assert harness.supervisor.degraded_reason is None
    harness.clock.advance(5.0)  # the whole window slides out
    harness.supervisor.tick()  # spawn #3
    harness.supervisor.tick()  # crash 3 — only one crash in the window now
    assert harness.supervisor.degraded_reason is None


def test_deferred_child_spawns_only_after_start_condition() -> None:
    ready = {"value": False}

    def condition() -> bool:
        return ready["value"]

    spec = ChildSpec(name="saq-runs", argv_builder=lambda: ["saq-worker"], start_condition=condition)
    harness = Harness(spec=spec)
    harness.supervisor.tick()
    assert not harness.spawned_argv
    ready["value"] = True
    harness.supervisor.tick()
    assert len(harness.spawned_argv) == 1


def test_probe_failure_after_health_restarts_through_crash_path() -> None:
    probe_results = [True, False]

    def probe() -> bool:
        if probe_results:
            return probe_results.pop(0)
        return False

    proc = FakeProc(alive=True)
    spec = ChildSpec(name="postgres", argv_builder=lambda: ["postgres-child"], probe=probe)
    harness = Harness(spec=spec)
    harness.supervisor._children["postgres"].process = proc
    harness.supervisor.tick()  # probe True → healthy
    child = harness.supervisor._children["postgres"]
    assert child.healthy
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # probe False → terminated, awaiting the exit
    assert proc.terminated
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # the terminated process is polled → real crash
    assert harness.crashes == [("postgres", 7)]
    assert child.process is None


def test_probe_unavailable_never_terminates_a_healthy_child() -> None:
    """A broken probe TOOL (UNAVAILABLE) must be distinguished from child-down."""
    outcomes: list[bool | ProbeOutcome] = [True, ProbeOutcome.UNAVAILABLE, False, ProbeOutcome.UNAVAILABLE]

    def probe() -> bool | ProbeOutcome:
        return outcomes.pop(0)

    proc = AliveProc()
    spec = ChildSpec(name="postgres", argv_builder=lambda: ["postgres-child"], probe=probe)
    harness = Harness(spec=spec)
    harness.supervisor._children["postgres"].process = proc
    harness.supervisor.tick()  # healthy
    child = harness.supervisor._children["postgres"]
    assert child.healthy
    harness.supervisor.tick()  # probe tool broken → child untouched
    assert child.healthy
    assert not proc.terminated
    harness.supervisor.tick()  # real probe failure → normal restart path
    assert proc.terminated
    harness.supervisor.tick()  # probe broken again while terminating — child untouched
    assert child.healthy  # the UNAVAILABLE outcome never mutates the healthy flag
    assert not proc.killed


def test_probe_exception_maps_to_unavailable() -> None:
    def broken() -> bool | ProbeOutcome:
        raise RuntimeError("probe tool exploded")

    proc = AliveProc()
    spec = ChildSpec(name="postgres", argv_builder=lambda: ["postgres-child"], probe=broken)
    harness = Harness(spec=spec)
    harness.supervisor._children["postgres"].process = proc
    harness.supervisor.tick()
    assert not proc.terminated
    assert not harness.crashes


def test_never_healthy_child_is_terminated_at_the_startup_deadline() -> None:
    """A child that never passes its probe must not wedge the supervisor."""
    spec = ChildSpec(name="postgres", argv_builder=lambda: ["postgres-child"], probe=lambda: False)
    harness = Harness(
        knobs=SupervisorKnobs(
            restart_backoff_initial=0.01,
            restart_backoff_max=0.02,
            health_check_timeout=1.0,
            shutdown_grace_seconds=0.01,
        ),
        spec=spec,
    )
    harness.supervisor._spawner = lambda argv, env: AliveProc()  # type: ignore[method-assign]
    harness.supervisor.tick()  # spawn
    child = harness.supervisor._children["postgres"]
    assert child.process is not None
    harness.clock.advance(0.5)
    harness.supervisor.tick()  # within the startup deadline — still probed
    assert not child.terminating_since
    harness.clock.advance(0.6)  # past spawn_time + health_check_timeout
    harness.supervisor.tick()  # deadline missed → terminate
    assert child.terminating_since is not None
    proc = child.process
    assert proc is not None and proc.terminated
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # grace elapsed → kill escalation
    assert proc is not None and proc.killed


def test_probe_less_child_is_presumed_live_after_the_liveness_deadline() -> None:
    """SAQ children (probe=None) turn healthy by surviving the startup window."""
    spec = ChildSpec(name="saq-runs", argv_builder=lambda: ["saq-worker"])
    harness = Harness(
        knobs=SupervisorKnobs(health_check_timeout=1.0),
        spec=spec,
    )
    proc = AliveProc()
    harness.supervisor._spawner = lambda argv, env: proc  # type: ignore[method-assign]
    harness.supervisor.tick()  # alive but within the deadline → not yet presumed live
    child = harness.supervisor._children["saq-runs"]
    assert child.spawned_at is not None
    assert not child.healthy
    harness.clock.advance(1.1)
    harness.supervisor.tick()  # liveness deadline passed → presumed live
    assert child.healthy
    harness.clock.advance(5.0)
    harness.supervisor.tick()  # stays live; never terminated by the supervisor
    assert not proc.terminated
    assert child.process is proc


def test_tick_survives_an_unexpected_child_exception() -> None:
    """A raise inside the per-child tick body is logged, not fatal."""

    class ExplodingProc:
        pid = 7000

        def poll(self) -> int | None:
            raise RuntimeError("os race")

        def terminate(self) -> None:
            raise AssertionError("never reached")

        def kill(self) -> None:
            raise AssertionError("never reached")

        def wait(self, timeout: float | None = None) -> int:
            return 1

    spec = ChildSpec(name="postgres", argv_builder=lambda: ["postgres-child"])
    harness = Harness(spec=spec)
    harness.supervisor.tick()  # spawn
    child = harness.supervisor._children["postgres"]
    child.process = ExplodingProc()  # type: ignore[assignment]
    harness.supervisor.tick()  # must not raise
    # The OTHER child still gets supervised after the explosion.
    harness.supervisor.add(ChildSpec(name="redis", argv_builder=lambda: ["redis-child"]))
    harness.supervisor.tick()
    assert harness.supervisor._children["redis"].process is not None
    assert harness.supervisor.degraded_reason is None


def test_teardown_order_and_escalation_follows_priority() -> None:
    events: list[str] = []

    def spawn(argv: list[str], env: dict[str, str] | None) -> RecordingProc:
        return RecordingProc(argv[0], events)

    supervisor = Supervisor(
        SupervisorKnobs(shutdown_grace_seconds=0.01, pg_fast_shutdown_timeout=0.02),
        spawner=spawn,
        clock=FakeClock(),
        sleep=lambda _seconds: None,
    )
    supervisor.add(
        ChildSpec(
            name="postgres",
            argv_builder=lambda: ["postgres"],
            shutdown_priority=1,
            teardown_signal=int(signal.SIGINT),
            shutdown_escalation_timeout=0.02,
        )
    )
    supervisor.add(ChildSpec(name="redis", argv_builder=lambda: ["redis"], shutdown_priority=2))
    supervisor.add(ChildSpec(name="saq", argv_builder=lambda: ["saq"], shutdown_priority=0))
    for child in supervisor._children.values():
        child.process = spawn(child.spec.argv_builder(), None)
    supervisor.shutdown()
    assert events == ["term:saq", "wait:saq", "term:postgres", "wait:postgres", "term:redis", "wait:redis"]
    for child in supervisor._children.values():
        assert child.process is None


def test_teardown_escapes_to_kill_when_child_ignores_grace() -> None:
    events: list[str] = []

    class StubbornProc:
        def __init__(self) -> None:
            self.pid = 6000

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            events.append("term")

        def kill(self) -> None:
            events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed_flag():
                raise RuntimeError("child ignores the signal")
            events.append("reaped")
            return 0

        def killed_flag(self) -> bool:
            return "kill" in events

    supervisor = Supervisor(
        SupervisorKnobs(shutdown_grace_seconds=0.01, pg_fast_shutdown_timeout=0.01),
        spawner=lambda argv, env: StubbornProc(),
        clock=FakeClock(),
        sleep=lambda _seconds: None,
    )
    supervisor.add(ChildSpec(name="sleeper", argv_builder=lambda: ["sleeper"]))
    supervisor._children["sleeper"].process = StubbornProc()
    supervisor.shutdown()
    assert events == ["term", "term", "kill", "reaped"]
    assert supervisor._children["sleeper"].process is None


# ---------------------------------------------------------------------------
# Data-dir lock
# ---------------------------------------------------------------------------


@requires_posix
def test_lock_refuses_second_holder_naming_pid_and_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = DataDirLock(data_dir, mode="serve")
    first.acquire()
    second = DataDirLock(data_dir, mode="doctor")
    with pytest.raises(DataDirLockError) as excinfo:
        second.acquire()
    message = str(excinfo.value)
    assert f"holder PID {os.getpid()}" in message
    assert "mode 'doctor'" in message
    first.release()


@requires_posix
def test_lock_release_allows_reacquire(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    lock.release()
    again = DataDirLock(data_dir, mode="serve")
    again.acquire()
    holder = again.holder
    assert holder is not None
    assert holder.pid == os.getpid()
    again.release()


@requires_posix
def test_lock_records_holder_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    holder = lock.holder
    assert holder is not None
    assert holder.pid == os.getpid()
    assert holder.mode == "serve"
    lock.release()


@requires_posix
def test_lock_records_holder_starttime_for_identity(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    holder = lock.holder
    assert holder is not None
    expected = read_proc_starttime(os.getpid())
    if sys.platform == "linux":
        assert holder.starttime == expected
        assert holder.starttime is not None
    else:
        assert holder.starttime is None
    lock.release()


@requires_posix
def test_lock_release_clears_the_holder_record(tmp_path: Path) -> None:
    """A clean release truncates the holder JSON — no stale identity survives."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    lock.release()
    assert lock.holder is None
    assert not (data_dir.parent / (data_dir.name + ".lock")).read_text(encoding="utf-8")


_LOCK_CHILD_SCRIPT = """
import sys
from pathlib import Path
from modulo.launcher.supervisor import DataDirLock

lock = DataDirLock(Path(sys.argv[1]), mode='serve')
lock.acquire()
Path(sys.argv[2]).write_text('locked')
import time
time.sleep(8)
"""


@requires_posix
def test_lock_is_kernel_released_when_holder_dies(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    marker = tmp_path / "locked.marker"
    # The child holds the lock until the run timeout SIGKILLs it mid-hold;
    # the marker proves it had acquired before it died (no stdout handshake).
    # If it dies early (e.g. a broken child script) the captured stderr is
    # surfaced instead of silently passing a no-timeout run.
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        subprocess.run(  # noqa: S603 — test driver
            [sys.executable, "-c", _LOCK_CHILD_SCRIPT, str(data_dir), str(marker)],
            timeout=5.0,
            capture_output=True,
            check=False,
        )
    assert marker.exists(), f"lock-holder child exited before the timeout — its stderr: {excinfo.value.stderr!r}"
    second = DataDirLock(data_dir, mode="serve")
    second.acquire()
    holder = second.holder
    assert holder is not None
    assert holder.pid == os.getpid()
    second.release()


def test_lock_refuses_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with pytest.raises(DataDirLockError, match=r"TODO\(P3\)"):
        DataDirLock(data_dir, mode="serve").acquire()


@requires_posix
def test_request_stop_signals_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    sent: list[int] = []

    def fake_kill(pid: int, signum: int) -> None:
        sent.append(signum)
        lock.release()  # the launcher releases the lock on SIGTERM

    monkeypatch.setattr(os, "kill", fake_kill)
    assert request_stop(data_dir, timeout=1.0) == 0
    assert sent == [signal.SIGTERM]


@requires_posix
def test_request_stop_without_holder_is_noop(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert request_stop(data_dir, timeout=0.5) == 0


@requires_posix
def test_request_stop_refuses_recycled_holder_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recycled PID is never SIGTERMed — the STARTTIME must match."""
    from modulo.launcher import supervisor as supervisor_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock = DataDirLock(data_dir, mode="serve")
    lock.acquire()
    holder = lock.holder
    assert holder is not None
    # Rewrite the recorded starttime so the LIVE process no longer matches
    # (the PID was "reused" by another process since acquire).
    payload = {
        "pid": holder.pid,
        "mode": holder.mode,
        "acquired_at": holder.acquired_at,
        "starttime": (holder.starttime or 0) + 10_000,
    }
    lock_path = data_dir.parent / (data_dir.name + ".lock")
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    sent: list[tuple[int, int]] = []

    def fake_kill(pid: int, signum: int) -> None:
        sent.append((pid, signum))

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(supervisor_module, "_lock_is_free", lambda _path: False)
    with pytest.raises(LauncherError, match="was reused"):
        request_stop(data_dir, timeout=0.5)
    assert sent == []
    lock.release()


@requires_posix
def test_request_stop_treats_dead_recorded_holder_as_stopped(tmp_path: Path) -> None:
    """A recorded holder whose STARTTIME is gone (process dead) is not signalled."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dead = _dead_pid()
    payload = {"pid": dead, "mode": "serve", "acquired_at": 1.0, "starttime": 42}
    lock_path = data_dir.parent / (data_dir.name + ".lock")
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    assert request_stop(data_dir, timeout=0.5) == 0


@requires_posix
def test_request_stop_reports_unverifiable_holder_still_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy lock file (no starttime) + held lock → still verified via flock."""
    from modulo.launcher import supervisor as supervisor_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    payload = {"pid": os.getpid(), "mode": "serve", "acquired_at": 1.0}
    lock_path = data_dir.parent / (data_dir.name + ".lock")
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(supervisor_module, "_lock_is_free", lambda _path: False)

    def fake_kill(pid: int, signum: int) -> None:
        pass

    monkeypatch.setattr(os, "kill", fake_kill)
    assert request_stop(data_dir, timeout=0.3) == 1


# ---------------------------------------------------------------------------
# Orphan reconciliation
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    output = subprocess.check_output(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        timeout=15,
        text=True,
    )
    return int(output.strip())


def test_reconcile_removes_dead_postmaster_pidfile(tmp_path: Path) -> None:
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text(f"{_dead_pid()}\n/pgdata\n1000.0\n5432\n")
    action = reconcile_orphans(pgdata)
    assert action == "removed_stale_postmaster_pid"
    assert not (pgdata / "postmaster.pid").exists()


def test_reconcile_refuses_live_postgres_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.launcher import supervisor as supervisor_module

    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text(f"{os.getpid()}\n/pgdata\n12345.0\n5432\n")
    monkeypatch.setattr(supervisor_module, "_pid_starttime_epoch", lambda _pid: 12345.0)
    monkeypatch.setattr(supervisor_module, "_is_postgres_process", lambda _pid: True)
    with pytest.raises(LauncherError, match="live postgres"):
        reconcile_orphans(pgdata)
    assert (pgdata / "postmaster.pid").exists()


def test_reconcile_treats_pid_reuse_as_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.launcher import supervisor as supervisor_module

    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text(f"{os.getpid()}\n/pgdata\n100000000.0\n5432\n")
    monkeypatch.setattr(supervisor_module, "_pid_starttime_epoch", lambda _pid: 12345.0)
    action = reconcile_orphans(pgdata)
    assert action == "removed_stale_postmaster_pid"
    assert not (pgdata / "postmaster.pid").exists()


def test_reconcile_sweeps_sigkill_debris(tmp_path: Path) -> None:
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    debris = tmp_path / ".initdb-tmp-stale"
    debris.mkdir()
    pwfile = tmp_path / ".initdb-pwfile-stale"
    pwfile.write_text("leaked-password")
    action = reconcile_orphans(pgdata)
    assert action == "swept_debris"
    assert not debris.exists()
    assert not pwfile.exists()


def test_reconcile_missing_pgdata_is_noop(tmp_path: Path) -> None:
    assert reconcile_orphans(tmp_path / "missing") == "swept_debris"


def test_reconcile_sweeps_stale_redis_conf_files(tmp_path: Path) -> None:
    from modulo.launcher.supervisor import REDIS_CONF_PREFIX

    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    stale = tmp_path / f"{REDIS_CONF_PREFIX}abc123"
    stale.write_text("requirepass leaked", encoding="utf-8")
    assert reconcile_orphans(pgdata) == "swept_debris"
    assert not stale.exists()


def test_is_postgres_process_refuses_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Linux the identity is UNKNOWABLE (None), never 'not postgres'."""
    if sys.platform == "linux":
        pytest.skip("non-Linux refusal contract")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _is_postgres_process(os.getpid()) is None


# ---------------------------------------------------------------------------
# Runtime manifest + status + knobs
# ---------------------------------------------------------------------------


def test_runtime_manifest_roundtrip_and_corrupt_handling(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    write_runtime_manifest(path, {"postgres": 100, "redis": 200})
    assert read_runtime_manifest(path) == {"postgres": 100, "redis": 200}
    path.write_text("not json{")
    assert not read_runtime_manifest(path)


def test_record_runtime_persists_installed_bundle_pg_version(tmp_path: Path) -> None:
    """`_record_runtime_locked` must persist `installed_bundle_pg_version` into the
    runtime manifest's extra so doctor's downgrade/upgrade axis can fire (FAR-676
    review finding — the axis was never written in production)."""
    path = tmp_path / "runtime.json"
    supervisor = Supervisor(SupervisorKnobs(), runtime_path=path)
    supervisor._installed_bundle_pg_version = "16.4"
    supervisor._record_runtime_locked()
    written = path.read_text(encoding="utf-8")
    assert "installed_bundle_pg_version" in written
    assert "16.4" in written
    import json

    manifest = json.loads(written)
    assert manifest["children"] == {}
    assert manifest["extra"]["installed_bundle_pg_version"] == "16.4"


def test_record_runtime_degraded_preserves_extra(tmp_path: Path) -> None:
    """A later degraded write must not wipe the previously-persisted bundle version."""
    path = tmp_path / "runtime.json"
    supervisor = Supervisor(SupervisorKnobs(), runtime_path=path)
    supervisor._installed_bundle_pg_version = "16.4"
    supervisor._record_runtime_locked()
    supervisor._degrade_locked("boom")
    written = path.read_text(encoding="utf-8")
    manifest = json.loads(written)
    assert manifest["extra"]["degraded_reason"] == "boom"
    assert manifest["extra"]["installed_bundle_pg_version"] == "16.4"


def test_knobs_from_env_overrides_and_ignores_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_LAUNCHER_TICK_SECONDS", "0.5")
    monkeypatch.setenv("MODULO_LAUNCHER_CRASH_CAP", "9")
    monkeypatch.setenv("MODULO_LAUNCHER_CRASH_WINDOW_SECONDS", "not-a-number")
    knobs = knobs_from_env()
    assert knobs.tick_seconds == 0.5
    assert knobs.crash_cap == 9
    assert knobs.crash_window_seconds == 600.0


def test_knobs_from_env_rejects_nonfinite_and_out_of_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_LAUNCHER_TICK_SECONDS", "nan")
    monkeypatch.setenv("MODULO_LAUNCHER_SHUTDOWN_GRACE_SECONDS", "1e999")
    monkeypatch.setenv("MODULO_LAUNCHER_CRASH_CAP", "100000")
    monkeypatch.setenv("MODULO_LAUNCHER_RESTART_BACKOFF_INITIAL", "-3")
    knobs = knobs_from_env()
    assert knobs.tick_seconds == 1.0
    assert knobs.shutdown_grace_seconds == 10.0
    assert knobs.crash_cap == 5
    assert knobs.restart_backoff_initial == 1.0


def _write_bootstrapped_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    secrets = load_or_create(data_dir / "secrets.json")
    state = LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)
    save_state(state, data_dir / "state.json", secrets.state_hmac_key)
    return data_dir


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        monkeypatch.setattr(secrets_file_module, "assert_supported_platform", lambda: None)


def test_collect_status_uninitialized_dir(tmp_path: Path) -> None:
    status = collect_status(tmp_path / "data")
    assert status["initialized"] is False
    assert not status["components"]


def test_collect_status_never_creates_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A status query is strictly read-only: no secrets file, no state file."""
    if sys.platform == "win32":
        monkeypatch.setattr(secrets_file_module, "assert_supported_platform", lambda: None)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    status = collect_status(data_dir)
    assert status["initialized"] is False
    assert not (data_dir / "secrets.json").exists()
    assert not (data_dir / "state.json").exists()


def test_collect_status_reports_unreadable_secrets(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "secrets.json").write_text("corrupt{", encoding="utf-8")
    status = collect_status(data_dir)
    assert status["initialized"] is False
    assert "secrets unavailable" in status["error"]


def test_collect_status_full_payload(tmp_path: Path) -> None:
    data_dir = _write_bootstrapped_data_dir(tmp_path)
    write_runtime_manifest(data_dir / "runtime.json", {"postgres": _dead_pid()})
    status = collect_status(data_dir)
    assert status["initialized"] is True
    assert status["postgres_port"] == 15432
    components = status["components"]
    assert components["postgres"]["alive"] is False
    assert components["redis"]["alive"] is False
    assert components["api"]["port"] == 18000
    assert components["api"]["alive"] is False


def test_shim_should_abandon_matrix() -> None:
    assert _shim_should_abandon(recorded_starttime=5, current_starttime=None)
    assert not _shim_should_abandon(recorded_starttime=5, current_starttime=5)
    assert _shim_should_abandon(recorded_starttime=5, current_starttime=6)


@requires_posix
def test_read_proc_starttime_for_self_is_positive() -> None:
    starttime = read_proc_starttime(os.getpid())
    assert starttime is not None
    assert starttime > 0


def test_child_shim_argv_falls_back_without_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    argv = child_shim_argv(["redis-server", "--port", "1"])
    assert argv == ["redis-server", "--port", "1"]


def test_child_shim_argv_unwrapped_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """No /proc STARTTIME on macOS → an unwrapped argv (a wrapped shim would
    kill its child on the first watchdog tick)."""
    if sys.platform == "linux":
        pytest.skip("the shim wraps on Linux by design")
    monkeypatch.setattr(sys, "platform", "darwin")
    argv = child_shim_argv(["redis-server", "--port", "1"])
    assert argv == ["redis-server", "--port", "1"]


@requires_posix
def test_child_shim_argv_wraps_on_linux() -> None:
    if sys.platform != "linux":
        pytest.skip("Linux-only shim mechanics")
    argv = child_shim_argv(["redis-server", "--port", "1"], parent_pid=os.getpid())
    assert argv[:3] == [sys.executable, "-m", "modulo.launcher.supervisor"]


# ---------------------------------------------------------------------------
# Real-process supervisor tests (Linux CI; skipped elsewhere)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="shim mechanics need /proc (Linux)")
def test_real_teardown_leaves_no_orphans() -> None:
    supervisor = Supervisor(SupervisorKnobs(shutdown_grace_seconds=2.0, pg_fast_shutdown_timeout=2.0))
    sleep_argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    supervisor.add(ChildSpec(name="sleeper", argv_builder=lambda: sleep_argv))
    supervisor.start(start_monitor=False)
    child = supervisor._children["sleeper"]
    shim = child.process
    assert shim is not None
    service_pids: list[int] = []
    for _ in range(100):
        service_pids = _children_of(shim.pid)
        if service_pids:
            break
        time.sleep(0.05)
    assert service_pids
    supervisor.shutdown()
    assert shim.poll() is not None
    for pid in service_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.skipif(sys.platform != "linux", reason="shim mechanics need /proc (Linux)")
def test_shim_kills_service_when_parent_starttime_missing() -> None:
    shim_argv = child_shim_argv([sys.executable, "-c", "import time; time.sleep(60)"], parent_pid=os.getpid())
    index = shim_argv.index("--parent-starttime")
    shim_argv[index + 1] = "-1"  # never matches the live parent = PID-reuse signal
    # The shim's first watchdog tick fires immediately (next_watchdog=0), so
    # it must exit(2) long before the run timeout — a TimeoutExpired here IS
    # the failure signal for a broken watchdog.
    result = subprocess.run(shim_argv, timeout=30, capture_output=True, check=False)  # noqa: S603 — test driver
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# Terminal degraded persistence (FAR-674)
# ---------------------------------------------------------------------------


class TailedProc(FakeProc):
    """FakeProc exposing a captured stderr tail (like _TailProcess)."""

    def __init__(self, *, exit_code: int = 7, tail: str | None = None) -> None:
        super().__init__(exit_code=exit_code)
        self._tail = tail

    def stderr_tail(self) -> str | None:
        return self._tail


def _trip_the_cap(harness: Harness, *, cap: int = 3) -> None:
    """Drive spawn/crash cycles until the crash cap trips."""
    for _ in range(cap):
        harness.clock.advance(0.02)
        harness.supervisor.tick()  # spawn
        harness.supervisor.tick()  # crash


def test_crash_cap_degrade_persists_a_record(tmp_path: Path) -> None:
    """Teardown still runs AND the degrade (reason + crash ring) is persisted."""
    degraded_path = tmp_path / "data" / "degraded.json"
    degraded_path.parent.mkdir(parents=True)
    harness = Harness(
        knobs=SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=0.001,
            restart_backoff_max=0.003,
            crash_window_seconds=100.0,
            crash_cap=3,
        ),
        degraded_path=degraded_path,
        degraded_context=lambda: {"post_upgrade": True},
    )
    _trip_the_cap(harness)
    assert harness.supervisor.degraded_reason is not None
    assert len(harness.degraded) == 1
    persisted = json.loads(degraded_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 1
    assert "postgres" in persisted["reason"]
    assert persisted["post_upgrade"] is True
    crashes = persisted["crashes"]
    assert len(crashes) == 3
    assert all(crash["child"] == "postgres" and crash["exit_code"] == 7 for crash in crashes)


def test_crash_cap_degrade_without_a_path_skips_persistence(tmp_path: Path) -> None:
    """degraded_path=None (the pre-FAR-674 contract) writes nothing."""
    harness = Harness(
        knobs=SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=0.001,
            restart_backoff_max=0.003,
            crash_window_seconds=100.0,
            crash_cap=2,
        )
    )
    _trip_the_cap(harness, cap=2)
    assert harness.supervisor.degraded_reason is not None
    assert not (tmp_path / "degraded.json").exists()


def test_stderr_tail_is_captured_in_the_crash_record(tmp_path: Path) -> None:
    """A crash record carries the child's captured stderr (the traceback)."""
    degraded_path = tmp_path / "data" / "degraded.json"
    degraded_path.parent.mkdir(parents=True)
    harness = Harness(
        knobs=SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=0.001,
            restart_backoff_max=0.003,
            crash_window_seconds=100.0,
            crash_cap=2,
        ),
        degraded_path=degraded_path,
    )

    class TraceProc(TailedProc):
        def __init__(self) -> None:
            super().__init__(exit_code=7, tail="Traceback (most recent call last): boom")

    harness.supervisor._spawner = lambda argv, env: TraceProc()  # type: ignore[method-assign]
    _trip_the_cap(harness, cap=2)
    persisted = json.loads(degraded_path.read_text(encoding="utf-8"))
    assert all("Traceback" in crash["backtrace"] for crash in persisted["crashes"])


def test_crash_ring_is_bounded_to_the_policy_size(tmp_path: Path) -> None:
    """Only the last DEGRADED_BACKTRACE_RING records land in the record."""
    degraded_path = tmp_path / "data" / "degraded.json"
    degraded_path.parent.mkdir(parents=True)
    harness = Harness(
        knobs=SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=0.001,
            restart_backoff_max=0.002,
            crash_window_seconds=10_000.0,
            crash_cap=1_000,
        ),
        degraded_path=degraded_path,
    )
    harness.supervisor.tick()  # spawn #1
    for _ in range(20):
        harness.clock.advance(0.02)
        harness.supervisor.tick()  # record crash
        harness.supervisor.tick()  # respawn (cap 1000 far away)
    assert len(harness.supervisor._crash_records) == policy_module.DEGRADED_BACKTRACE_RING
    harness.supervisor._degrade_locked("forced")
    persisted = json.loads(degraded_path.read_text(encoding="utf-8"))
    assert len(persisted["crashes"]) == policy_module.DEGRADED_BACKTRACE_RING


def test_supervisor_internal_exception_lands_in_the_ring(tmp_path: Path) -> None:
    """An argv-builder failure is a recorded supervisor-side crash (traceback)."""
    degraded_path = tmp_path / "data" / "degraded.json"
    degraded_path.parent.mkdir(parents=True)

    def exploding_argv() -> list[str]:
        raise RuntimeError("spawn mechanism broken")

    harness = Harness(
        knobs=SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=0.01,
            restart_backoff_max=0.02,
            crash_window_seconds=100.0,
            crash_cap=2,
        ),
        spec=ChildSpec(name="postgres", argv_builder=exploding_argv),
        degraded_path=degraded_path,
    )
    harness.supervisor.tick()  # argv_builder raised → ring record, no spawn
    assert not harness.spawned_argv
    harness.supervisor._degrade_locked("forced")
    persisted = json.loads(degraded_path.read_text(encoding="utf-8"))
    assert any(crash["backtrace"] and "spawn mechanism broken" in crash["backtrace"] for crash in persisted["crashes"])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_degraded_record_is_written_0600(tmp_path: Path) -> None:
    degraded_path = tmp_path / "data" / "degraded.json"
    degraded_path.parent.mkdir(parents=True)
    harness = Harness(
        knobs=SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=0.001,
            restart_backoff_max=0.003,
            crash_window_seconds=100.0,
            crash_cap=1,
        ),
        degraded_path=degraded_path,
    )
    harness.supervisor.tick()
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # crash 1 == cap → degrade + persist
    mode = degraded_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_degraded_record_readers_are_tolerant(tmp_path: Path) -> None:
    from modulo.launcher.supervisor import (
        clear_degraded_record,
        read_degraded_record,
        write_degraded_record,
    )

    path = tmp_path / "degraded.json"
    assert read_degraded_record(path) is None  # missing = not degraded
    assert clear_degraded_record(path) is False  # nothing to clear
    write_degraded_record(path, {"schema_version": 1, "degraded_at": 1.0, "reason": "cap"})
    assert read_degraded_record(path) == {"schema_version": 1, "degraded_at": 1.0, "reason": "cap"}
    path.write_text("{corrupt", encoding="utf-8")
    assert read_degraded_record(path) is None  # torn file must never wedge boot
    path.write_text('{"schema_version": 1, "future_field": [1]}', encoding="utf-8")
    assert read_degraded_record(path) is None  # no reason -> not a degrade record
    assert clear_degraded_record(path) is True


def test_collect_status_surfaces_the_degraded_record(tmp_path: Path) -> None:
    from modulo.launcher.supervisor import DEGRADED_FILENAME, write_degraded_record

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_degraded_record(
        data_dir / DEGRADED_FILENAME,
        {"schema_version": 1, "degraded_at": 5.0, "reason": "cap", "crashes": [{"child": "redis"}]},
    )
    status = collect_status(data_dir)
    assert status["degraded"]["reason"] == "cap"
    assert status["degraded"]["crashes"] == [{"child": "redis"}]


# ---------------------------------------------------------------------------
# Policy single-sourcing (FAR-674)
# ---------------------------------------------------------------------------


def test_supervisor_knob_defaults_are_the_policy_constants() -> None:
    """SupervisorKnobs must bind the policy constants, not restate copies."""
    defaults = SupervisorKnobs()
    assert defaults.tick_seconds == policy_module.TICK_SECONDS
    assert defaults.restart_backoff_initial == policy_module.RESTART_BACKOFF_INITIAL
    assert defaults.restart_backoff_max == policy_module.RESTART_BACKOFF_MAX
    assert defaults.crash_window_seconds == policy_module.CRASH_WINDOW_SECONDS
    assert defaults.crash_cap == policy_module.CRASH_CAP
    assert defaults.pg_fast_shutdown_timeout == policy_module.PG_FAST_SHUTDOWN_TIMEOUT
    assert defaults.shutdown_grace_seconds == policy_module.SHUTDOWN_GRACE_SECONDS
    assert defaults.health_check_timeout == policy_module.HEALTH_CHECK_TIMEOUT
    assert defaults.health_check_interval == policy_module.HEALTH_CHECK_INTERVAL


def test_policy_shell_constants_generation() -> None:
    generated = policy_module.format_shell_constants()
    assert f"SLIDING_WINDOW_S={int(policy_module.SLIDING_WINDOW_SECONDS)}" in generated
    assert f"SLIDING_CRASH_LIMIT={policy_module.SLIDING_CRASH_LIMIT}" in generated
    assert f"SLIDING_RESTART_SLEEP_S={int(policy_module.SLIDING_RESTART_SLEEP_SECONDS)}" in generated
    assert f"SUPERVISOR_CRASH_CAP={policy_module.CRASH_CAP}" in generated
    for line in generated.splitlines():
        stripped = line.strip()
        assert not stripped or stripped.startswith("#") or "=" in stripped


def test_entrypoint_sources_the_policy_constants() -> None:
    """The entrypoint consumes the generated constants (no inline magic left).

    Verifies the sourced values equal the Python constants by running the
    generator exactly as the entrypoint does, and greps the script for the
    wiring.
    """
    repo_root = Path(__file__).resolve().parents[4]
    entrypoint = repo_root / "deploy" / "fly" / "entrypoint.sh"
    assert entrypoint.exists(), f"entrypoint missing at {entrypoint}"
    text = entrypoint.read_text(encoding="utf-8")
    assert "SLIDING_WINDOW_S=300" not in text  # inline magic numbers removed
    assert "SLIDING_CRASH_LIMIT=5" not in text
    assert "modulo.launcher.policy" in text
    assert "SLIDING_RESTART_SLEEP_S" in text

    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "modulo.launcher.policy"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(repo_root / "backend" / "src")},
    )
    assert result.returncode == 0, result.stderr
    sourced: dict[str, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        sourced[key] = value
    assert int(sourced["SLIDING_WINDOW_S"]) == int(policy_module.SLIDING_WINDOW_SECONDS)
    assert int(sourced["SLIDING_CRASH_LIMIT"]) == policy_module.SLIDING_CRASH_LIMIT
    assert int(sourced["SLIDING_RESTART_SLEEP_S"]) == int(policy_module.SLIDING_RESTART_SLEEP_SECONDS)
    assert str(policy_module.CRASH_CAP) == sourced["SUPERVISOR_CRASH_CAP"]
