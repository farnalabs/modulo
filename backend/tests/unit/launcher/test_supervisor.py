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

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import modulo.launcher.secrets_file as secrets_file_module
from modulo.launcher.secrets_file import load_or_create
from modulo.launcher.state import LauncherState, save_state
from modulo.launcher.supervisor import (
    ChildSpec,
    DataDirLock,
    DataDirLockError,
    LauncherError,
    Supervisor,
    SupervisorKnobs,
    _children_of,
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


class Harness:
    """Manual-clock supervisor with recorded spawns/hooks (no threads)."""

    def __init__(self, knobs: SupervisorKnobs | None = None, spec: ChildSpec | None = None) -> None:
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
    # Spawn, then four crash cycles (window 2s, cap 3 → the 4th trip).
    harness.supervisor.tick()  # spawn #1
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # crash 1 → respawn scheduled at +0.01
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # spawn #2
    harness.supervisor.tick()  # crash 2
    harness.clock.advance(0.03)
    harness.supervisor.tick()  # spawn #3
    harness.supervisor.tick()  # crash 3 (== cap, no trip yet)
    assert harness.supervisor.degraded_reason is None
    harness.clock.advance(0.03)
    harness.supervisor.tick()  # spawn #4
    harness.supervisor.tick()  # crash 4 → cap tripped
    assert harness.supervisor.degraded_reason is not None
    assert "postgres" in harness.supervisor.degraded_reason
    assert len(harness.degraded) == 1
    assert len(harness.crashes) == 4
    spawns_after_degrade = len(harness.spawned_argv)
    harness.clock.advance(1.0)
    harness.supervisor.tick()
    assert len(harness.spawned_argv) == spawns_after_degrade


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
    harness.supervisor.tick()  # crash 2
    harness.clock.advance(0.02)
    harness.supervisor.tick()  # spawn #3
    harness.supervisor.tick()  # crash 3 — at the cap, not over it
    assert harness.supervisor.degraded_reason is None
    harness.clock.advance(5.0)  # the whole window slides out
    harness.supervisor.tick()  # spawn #4
    harness.supervisor.tick()  # crash 4 — only one crash in the window now
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


_LOCK_CHILD_SCRIPT = """
import sys
from pathlib import Path
from modulo.launcher.supervisor import DataDirLock

lock = DataDirLock(sys.argv[1], mode='serve')
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
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603 — test driver
            [sys.executable, "-c", _LOCK_CHILD_SCRIPT, str(data_dir), str(marker)],
            timeout=5.0,
            capture_output=True,
            check=False,
        )
    assert marker.exists()
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


# ---------------------------------------------------------------------------
# Runtime manifest + status + knobs
# ---------------------------------------------------------------------------


def test_runtime_manifest_roundtrip_and_corrupt_handling(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    write_runtime_manifest(path, {"postgres": 100, "redis": 200})
    assert read_runtime_manifest(path) == {"postgres": 100, "redis": 200}
    path.write_text("not json{")
    assert not read_runtime_manifest(path)


def test_knobs_from_env_overrides_and_ignores_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_LAUNCHER_TICK_SECONDS", "0.5")
    monkeypatch.setenv("MODULO_LAUNCHER_CRASH_CAP", "9")
    monkeypatch.setenv("MODULO_LAUNCHER_CRASH_WINDOW_SECONDS", "not-a-number")
    knobs = knobs_from_env()
    assert knobs.tick_seconds == 0.5
    assert knobs.crash_cap == 9
    assert knobs.crash_window_seconds == 600.0


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
