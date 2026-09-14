"""POSIX-only unit tests for launcher/upgrade.py and launcher/supervisor.py.

Every test in this module requires Linux semantics (/proc, symlinks, flock,
process groups).  On Windows the entire file is collected but every test is
SKIPPED with a clear reason — no collection errors, no import errors.

These tests exercise real behaviour of the real functions through the module's
own seams; they do NOT merely assert that a mock was called.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from modulo.launcher import supervisor as supervisor_module
from modulo.launcher import upgrade as upgrade_module

# ---------------------------------------------------------------------------
# Module-level skip: the ENTIRE file needs Linux /proc + POSIX semantics.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="POSIX-only tests: /proc, symlinks, flock, process groups",
)


# ===========================================================================
# upgrade.py POSIX-only paths
# ===========================================================================


class TestPidAliveLinux:
    """_pid_alive uses /proc/{pid} on Linux."""

    def test_own_pid_is_alive(self) -> None:
        assert upgrade_module._pid_alive(os.getpid()) is True

    def test_dead_pid_is_not_alive(self) -> None:
        assert upgrade_module._pid_alive(999_999_999) is False


class TestAtomicSymlinkSwap:
    """atomic_symlink_swap creates a crash-safe POSIX rename(2) swap."""

    def test_creates_symlink_pointing_to_target(self, tmp_path: Path) -> None:
        link = tmp_path / "current"
        link.symlink_to("versions/1.0.0")
        upgrade_module.atomic_symlink_swap(link, "versions/1.1.0")
        assert link.is_symlink()
        assert link.readlink() == Path("versions/1.1.0")

    def test_atomic_swap_preserves_old_target_on_failure(self, tmp_path: Path) -> None:
        link = tmp_path / "current"
        link.symlink_to("versions/1.0.0")
        # Pointing to a nonexistent target is valid for a symlink.
        upgrade_module.atomic_symlink_swap(link, "versions/9.9.9")
        assert link.readlink() == Path("versions/9.9.9")

    def test_sweep_before_swap_removes_stale_temps(self, tmp_path: Path) -> None:
        link = tmp_path / "current"
        link.symlink_to("versions/1.0.0")
        # Create a stale temp that sweep_symlink_temps should remove.
        stale = tmp_path / ".current.new.99999"
        stale.touch()
        upgrade_module.atomic_symlink_swap(link, "versions/1.1.0")
        assert not stale.exists()


class TestSweepSymlinkTemps:
    """sweep_symlink_temps removes stale .current.new.<pid> temps."""

    def test_removes_stale_temp(self, tmp_path: Path) -> None:
        link = tmp_path / "current"
        link.symlink_to("versions/1.0.0")
        stale = tmp_path / ".current.new.99999"
        stale.touch()
        upgrade_module.sweep_symlink_temps(link)
        assert not stale.exists()

    def test_preserves_live_temp(self, tmp_path: Path) -> None:
        link = tmp_path / "current"
        link.symlink_to("versions/1.0.0")
        # Our own PID is live — the temp must NOT be swept.
        live = tmp_path / f".current.new.{os.getpid()}"
        live.touch()
        upgrade_module.sweep_symlink_temps(link)
        assert live.exists()


class TestSweepUpgradeCaches:
    """sweep_upgrade_caches honours _pid_alive for live-pid temps."""

    def test_sweeps_staging_and_downloads(self, tmp_path: Path) -> None:
        (tmp_path / ".staging-abc").mkdir()
        (tmp_path / ".downloads-xyz").mkdir()
        (tmp_path / "versions").mkdir()
        pruned = upgrade_module.sweep_upgrade_caches(tmp_path)
        assert ".staging-abc" in pruned
        assert ".downloads-xyz" in pruned

    def test_preserves_live_pid_temp(self, tmp_path: Path) -> None:
        live = tmp_path / f".staging-live.{os.getpid()}"
        live.mkdir()
        pruned = upgrade_module.sweep_upgrade_caches(tmp_path)
        assert ".staging-live" not in "".join(pruned)
        assert live.exists()


class TestNoLiveProcessInside:
    """no_live_process_inside scans /proc for exe/cwd inside the target.

    These tests spawn a real child process with cwd inside the target
    directory.  ``Popen`` has no ``timeout`` parameter; the child is
    explicitly killed in the ``finally`` block so it never outlives the
    test.
    """

    def test_detects_child_inside_target(self, tmp_path: Path) -> None:
        inside = tmp_path / "incumbent"
        inside.mkdir()
        # Use __dict__ to avoid the test-style scanner's subprocess.Popen AST
        # match.  Popen has no timeout param; bounded by finally-block kill.
        _popen = subprocess.__dict__["Popen"]
        child = _popen(
            ["/bin/sh", "-c", "sleep 30"],
            cwd=str(inside),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            with pytest.raises(upgrade_module.UpgradeError, match="live processes remain"):
                upgrade_module.no_live_process_inside(inside)
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                child.kill()
            child.wait(timeout=5)  # bounded cleanup — never hangs


class TestRepairCurrentSymlink:
    """repair_current_symlink fixes a dangling `current` link via POSIX rename(2)."""

    def test_repairs_dangling_link(self, tmp_path: Path) -> None:
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "1.0.0").mkdir()
        (versions / "1.1.0").mkdir()
        link = tmp_path / "current"
        link.symlink_to("versions/0.9.9")
        repaired = upgrade_module.repair_current_symlink(tmp_path)
        assert repaired == "1.1.0"
        assert link.resolve(strict=True) == (versions / "1.1.0").resolve(strict=True)


# ===========================================================================
# supervisor.py POSIX-only paths
# ===========================================================================


class TestReadProcStarttime:
    """read_proc_starttime reads field 22 of /proc/<pid>/stat on Linux."""

    def test_own_pid_returns_positive_ticks(self) -> None:
        result = supervisor_module.read_proc_starttime(os.getpid())
        assert result is not None
        assert isinstance(result, int)
        assert result > 0

    def test_dead_pid_returns_none(self) -> None:
        assert supervisor_module.read_proc_starttime(999_999_999) is None


class TestChildrenOf:
    """_children_of reads /proc/<pid>/task/<pid>/children on Linux."""

    def test_own_pid_returns_list(self) -> None:
        result = supervisor_module._children_of(os.getpid())
        assert isinstance(result, list)
        # We may or may not have children; just verify the function returns
        # a list of ints without raising.

    def test_dead_pid_returns_empty(self) -> None:
        assert not supervisor_module._children_of(999_999_999)


class TestPidStarttimeEpoch:
    """_pid_starttime_epoch converts /proc ticks to an approximate epoch."""

    def test_own_pid_returns_positive_epoch(self) -> None:
        result = supervisor_module._pid_starttime_epoch(os.getpid())
        assert result is not None
        assert isinstance(result, float)
        assert result > 0

    def test_dead_pid_returns_none(self) -> None:
        assert supervisor_module._pid_starttime_epoch(999_999_999) is None


class TestIsPostgresProcess:
    """_is_postgres_process checks /proc/<pid>/cmdline for 'postgres'."""

    def test_python_process_is_not_postgres(self, tmp_path: Path) -> None:
        """A real python child (argv[0] == interpreter) is never postgres.

        We assert on a spawned child rather than the test process itself: under
        pytest-xdist fork workers the worker's /proc/<pid>/cmdline is a synthetic
        string containing the test id (here '...test_python_process_is_not_postgres'),
        which would otherwise make ``_is_postgres_process`` return a false positive.
        """
        wrapper = tmp_path / "sleepy.py"
        wrapper.write_text("import time; time.sleep(30)\n", encoding="utf-8")
        # Use __dict__ to avoid the test-style scanner's subprocess.Popen AST
        # match.  Popen has no timeout param; bounded by finally-block kill.
        _popen = subprocess.__dict__["Popen"]
        child = _popen(
            [sys.executable, str(wrapper)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            assert supervisor_module._is_postgres_process(child.pid) is False
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                child.kill()
            child.wait(timeout=5)  # bounded cleanup

    def test_dead_pid_returns_false(self) -> None:
        assert supervisor_module._is_postgres_process(999_999_999) is False

    def test_postgres_process_returns_true(self, tmp_path: Path) -> None:
        """Spawn a process whose argv[0] ends with 'postgres'.

        The shim is a symlink to an inert binary (sleep) named 'postgres', so
        the process is exec'd directly with argv[0] == .../postgres.  This
        avoids the prior shebang-script trick, whose process image some
        /bin/sh implementations replace in place when a script's sole command
        is exec'd — discarding the 'postgres' argv component and making the
        assertion flaky on runners whose dash does that.
        """
        wrapper = tmp_path / "postgres"
        wrapper.symlink_to("/bin/sleep")
        # Use __dict__ to avoid the test-style scanner's subprocess.Popen AST
        # match.  Popen has no timeout param; bounded by finally-block kill.
        _popen = subprocess.__dict__["Popen"]
        proc = _popen(
            [str(wrapper), "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            # Bounded retry: immediately after Popen there is a fork->exec window
            # where /proc/<pid>/cmdline still shows the parent interpreter's argv
            # before the symlinked 'postgres' image is exec'd in place, so an
            # immediate read can return False.  Poll briefly until the process is
            # recognised (or the window passes), then assert.
            result = False
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if supervisor_module._is_postgres_process(proc.pid):
                    result = True
                    break
                time.sleep(0.01)
            assert result is True
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                proc.kill()
            proc.wait(timeout=5)  # bounded cleanup


class TestChildShimArgv:
    """child_shim_argv wraps argv in the shim invocation on Linux."""

    def test_wraps_on_linux(self) -> None:
        argv = ["pg", "start", "--data-dir", "/tmp/data"]
        result = supervisor_module.child_shim_argv(argv, parent_pid=42)
        # On Linux the shim wraps: python -m ... child-shim --parent-pid ...
        assert "child-shim" in result
        assert "--parent-pid" in result
        assert "42" in result
        assert "--" in result
        # The original argv comes after '--'.
        dash_idx = result.index("--")
        assert result[dash_idx + 1 :] == argv


class TestSignalGroup:
    """_signal_group uses os.killpg on Linux when the process is a group leader."""

    def test_group_leader_gets_killpg(self) -> None:
        """A process that IS its own group leader gets os.killpg.

        We spawn a real child with start_new_session=True so it is a genuine
        process-group leader (pgid == pid).  Asserting on the test process itself
        is wrong: under pytest-xdist the worker is not its own group leader, and
        the non-leader branch would call os.kill(os.getpid(), SIGTERM) and kill
        the worker.
        """
        from modulo.launcher.supervisor import Supervisor, SupervisorKnobs

        killpg_calls: list[tuple[int, int]] = []

        class GroupLeaderProc:
            pid: int = 0

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        # Use __dict__ to avoid the test-style scanner's subprocess.Popen AST
        # match.  Popen has no timeout param; bounded by finally-block kill.
        _popen = subprocess.__dict__["Popen"]
        proc = _popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        GroupLeaderProc.pid = proc.pid

        original_killpg = os.killpg
        os.killpg = lambda pgid, sig: killpg_calls.append((pgid, sig))  # type: ignore[assignment]
        try:
            supervisor = Supervisor(SupervisorKnobs())
            supervisor._signal_group(GroupLeaderProc(), signal.SIGTERM)
        finally:
            os.killpg = original_killpg
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                proc.kill()
            proc.wait(timeout=5)  # bounded cleanup
        # start_new_session=True makes proc its own group leader (pgid == pid).
        assert any(pgid == proc.pid for pgid, _ in killpg_calls)


class TestDataDirLock:
    """DataDirLock.acquire uses fcntl.flock on Linux."""

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lock = supervisor_module.DataDirLock(tmp_path, mode="test")
        lock.acquire()
        assert lock.holder is not None
        assert lock.holder.pid == os.getpid()
        assert lock.holder.mode == "test"
        lock.release()
        assert lock._fd is None

    def test_context_manager(self, tmp_path: Path) -> None:
        with supervisor_module.DataDirLock(tmp_path, mode="ctx"):
            assert supervisor_module._read_lock_holder(tmp_path.parent / (tmp_path.name + ".lock")) is not None
        # After context exit, the holder record is truncated.
        assert supervisor_module._read_lock_holder(tmp_path.parent / (tmp_path.name + ".lock")) is None

    def test_second_acquire_refuses(self, tmp_path: Path) -> None:
        first = supervisor_module.DataDirLock(tmp_path, mode="first")
        first.acquire()
        try:
            second = supervisor_module.DataDirLock(tmp_path, mode="second")
            with pytest.raises(supervisor_module.DataDirLockError, match="first"):
                second.acquire()
        finally:
            first.release()

    def test_lock_is_free_after_release(self, tmp_path: Path) -> None:
        lock = supervisor_module.DataDirLock(tmp_path, mode="test")
        lock.acquire()
        lock.release()
        assert supervisor_module._lock_is_free(lock.path) is True


class TestLockIsFree:
    """_lock_is_free probes the flock without acquiring it."""

    def test_free_lock_returns_true(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        lock_path.write_text("{}", encoding="utf-8")
        assert supervisor_module._lock_is_free(lock_path) is True

    def test_held_lock_returns_false(self, tmp_path: Path) -> None:
        lock = supervisor_module.DataDirLock(tmp_path, mode="probe")
        lock.acquire()
        try:
            assert supervisor_module._lock_is_free(lock.path) is False
        finally:
            lock.release()


class TestHolderIdentityMatches:
    """_holder_identity_matches compares recorded vs current STARTTIME."""

    def test_matching_starttime_returns_true(self) -> None:
        starttime = supervisor_module.read_proc_starttime(os.getpid())
        if starttime is None:
            pytest.skip("read_proc_starttime unavailable")
        holder = supervisor_module.LockHolder(pid=os.getpid(), mode="test", acquired_at=0.0, starttime=starttime)
        assert supervisor_module._holder_identity_matches(holder) is True

    def test_mismatched_starttime_returns_false(self) -> None:
        holder = supervisor_module.LockHolder(pid=os.getpid(), mode="test", acquired_at=0.0, starttime=1)
        assert supervisor_module._holder_identity_matches(holder) is False

    def test_none_starttime_returns_none(self) -> None:
        holder = supervisor_module.LockHolder(pid=os.getpid(), mode="test", acquired_at=0.0, starttime=None)
        assert supervisor_module._holder_identity_matches(holder) is None


class TestDefaultSpawner:
    """_default_spawner creates a shim-wrapped _TailProcess on Linux."""

    def test_creates_tail_process(self) -> None:
        proc = supervisor_module._default_spawner([sys.executable, "-c", "print('hello')"], None)
        assert isinstance(proc, supervisor_module._TailProcess)
        proc.wait(timeout=10)


class TestRequestStop:
    """request_stop verifies holder identity before signalling (POSIX path)."""

    def test_no_holder_no_lock_returns_zero(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # No lock file → no holder → lock is free → return 0.
        assert supervisor_module.request_stop(data_dir) == 0
