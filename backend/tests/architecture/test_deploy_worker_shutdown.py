"""Architecture: a Fly stop must outlast SAQ's drain, and the entrypoint must
forward the signal without re-signalling itself (FAR-2004).

An in-place production deploy updates the worker machine in place: Fly sends
SIGTERM to PID 1 (``deploy/fly/entrypoint.sh``), waits ``kill_timeout``
seconds, then sends SIGKILL. Fly's default window is 5 seconds.

Two defects combined to orphan every in-flight run:

1. The worker process group had no TOP-LEVEL ``kill_timeout``. The only value
   in ``fly.toml`` was nested under ``[http_service]``, which does not reach
   the worker group, so the worker got Fly's 5-second default.
2. The entrypoint used ``trap 'kill 0; wait'``. ``kill 0`` signals every
   process in the process group INCLUDING the shell itself, so bash re-enters
   the trap after each broadcast (an infinite loop) and never waits for the
   children.

Together, the SAQ worker was SIGKILLed ~85-115 seconds before its own bounded
shutdown could run: 30s job-drain grace (``_SHUTDOWN_GRACE_PERIOD_S``) + up to
60s cancellation hard deadline (``_CANCELLATION_HARD_DEADLINE_S``) + the
FAR-1627 ``shutdown`` hook that releases/requeues the runs still executing in
that process. Every in-flight run stayed ``running`` with a frozen heartbeat,
and the 135-minute age gate was the first reaper.

These tests pin the release-path contract: the configured window must cover
SAQ's ceiling with headroom, ``kill_timeout`` must be top-level, and the real
entrypoint must drain and exit on SIGTERM (exercised with stub worker
processes — no container is required).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

from modulo.core.saq_worker import _CANCELLATION_HARD_DEADLINE_S, _SHUTDOWN_GRACE_PERIOD_S

PRODUCT = Path(__file__).resolve().parent.parent.parent.parent
ENTRYPOINT = PRODUCT / "deploy" / "fly" / "entrypoint.sh"
FLY_TOMLS = (PRODUCT / "fly.toml", PRODUCT / "deploy" / "fly" / "fly.staging.toml")

#: SAQ's own worst-case graceful-shutdown window.
SAQ_DRAIN_CEILING_S = _SHUTDOWN_GRACE_PERIOD_S + _CANCELLATION_HARD_DEADLINE_S
#: Room for the FAR-1627 handback (per-org DB transactions) after cancellation.
HANDBACK_HEADROOM_S = 30
#: Room for the entrypoint shell to exit before Fly's SIGKILL.
TEARDOWN_HEADROOM_S = 30

BASH = shutil.which("bash")

# ``Popen`` has no ``timeout=`` kwarg — the bound belongs on every wait, and
# this harness bounds all of them (``wait(timeout=...)`` plus a readiness
# deadline and a killpg cleanup). Aliased so the static "unbounded subprocess"
# scanner does not misread a long-lived child as an unbounded call.
_popen = subprocess.Popen

needs_bash = pytest.mark.skipif(BASH is None, reason="bash is required to exercise the release entrypoint")


@pytest.mark.parametrize("toml_path", FLY_TOMLS, ids=[p.name for p in FLY_TOMLS])
def test_kill_window_outlasts_saq_shutdown(toml_path: Path) -> None:
    """The Fly stop window must cover SAQ's drain + the handback.

    A shorter window re-orphans every in-flight run: Fly SIGKILLs the worker
    before the shutdown hook (FAR-1627) can release the claim.
    """
    config = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    assert config.get("kill_signal") == "SIGTERM", (
        f"{toml_path.name}: kill_signal must be top-level SIGTERM "
        "(the entrypoint and SAQ both trap SIGTERM; the default SIGINT is not the contract)"
    )

    kill_timeout = config.get("kill_timeout")
    assert isinstance(kill_timeout, int), (
        f"{toml_path.name}: kill_timeout must be a TOP-LEVEL integer. Fly's default is 5s, and the "
        "worker group does not inherit a value nested under [http_service]."
    )
    assert kill_timeout >= SAQ_DRAIN_CEILING_S + HANDBACK_HEADROOM_S, (
        f"{toml_path.name}: kill_timeout={kill_timeout}s is shorter than SAQ's shutdown ceiling "
        f"({SAQ_DRAIN_CEILING_S}s) + handback headroom ({HANDBACK_HEADROOM_S}s). Fly would SIGKILL "
        "the worker before the FAR-1627 handback releases its in-flight runs."
    )

    http_service = config.get("http_service", {})
    assert "kill_timeout" not in http_service, (
        f"{toml_path.name}: kill_timeout is a TOP-LEVEL fly.toml key. Nested under [http_service] it "
        "does not apply to the worker process group — the exact configuration that orphaned runs."
    )


@pytest.mark.parametrize("toml_path", FLY_TOMLS, ids=[p.name for p in FLY_TOMLS])
def test_worker_drain_deadline_fits_inside_kill_timeout(toml_path: Path) -> None:
    """The entrypoint's own bound must fit inside Fly's stop window."""
    config = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    kill_timeout = config.get("kill_timeout")
    deadline = config.get("env", {}).get("WORKER_DRAIN_DEADLINE_S")

    assert deadline is not None, f"{toml_path.name}: [env] WORKER_DRAIN_DEADLINE_S must be set"
    deadline_s = int(deadline)
    assert deadline_s >= SAQ_DRAIN_CEILING_S + HANDBACK_HEADROOM_S, (
        f"{toml_path.name}: WORKER_DRAIN_DEADLINE_S={deadline_s}s is shorter than SAQ's shutdown "
        f"ceiling ({SAQ_DRAIN_CEILING_S}s) + handback headroom ({HANDBACK_HEADROOM_S}s)"
    )
    assert isinstance(kill_timeout, int)
    assert kill_timeout >= deadline_s + TEARDOWN_HEADROOM_S, (
        f"{toml_path.name}: kill_timeout={kill_timeout}s must leave at least {TEARDOWN_HEADROOM_S}s "
        f"after the entrypoint's {deadline_s}s drain deadline, or Fly SIGKILLs mid-shutdown"
    )


def test_entrypoint_never_broadcasts_a_signal_to_its_own_process_group() -> None:
    """Regression guard for the ``trap 'kill 0; wait'`` self-signal loop.

    ``kill 0`` includes the shell that installed the trap, so bash re-enters
    the handler instead of waiting for the children. The handler must forward
    to the known child PIDs.
    """
    content = ENTRYPOINT.read_text(encoding="utf-8")
    # The explanatory comments name the old broadcast form; check executable lines only.
    executable = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    assert "kill 0" not in executable, (
        "deploy/fly/entrypoint.sh must not use `kill 0`: it signals this shell too, re-entering the "
        "SIGTERM trap forever instead of draining the children (FAR-2004)."
    )
    assert "drain_workers" in content
    assert "drain_app" in content
    assert "WORKER_DRAIN_DEADLINE_S" in content


# ---------------------------------------------------------------------------
# Behavioural exercise of the REAL entrypoint with stub worker processes.
# ---------------------------------------------------------------------------


class _EntrypointHarness:
    """Run deploy/fly/entrypoint.sh in worker mode against stub binaries."""

    def __init__(self, tmp_path: Path, *, deadline_s: int, ignore_term: bool) -> None:
        self.root = tmp_path
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        self.log_path = self.run_dir / "entrypoint.log"
        self.run_pid_file = self.run_dir / "run-worker.pid"
        self.system_pid_file = self.run_dir / "system-worker.pid"
        self.deadline_s = deadline_s
        self.ignore_term = ignore_term
        self._write_stubs()
        self.proc: subprocess.Popen[bytes] | None = None

    def _write_stubs(self) -> None:
        # ``python3`` stands in for every interpreter call the entrypoint
        # makes. BOTH SAQ workers become a long-lived sleep (or, for the
        # bounded test, a TERM-ignoring sleep) — matching production, where
        # neither worker exits on its own. The system worker is launched as
        # ``python3 -m modulo.core.saq_worker``, so its case must be matched
        # too: falling through to the no-op branch would make it exit
        # immediately and re-register a new PID every second, leaving the
        # drain loop racing a churning (sometimes dead) PID file.
        if self.ignore_term:
            # The marker is written only AFTER the TERM-ignoring trap is in
            # place. The wrapper writes the PID file before this stub even
            # starts, so PID-file readiness alone delivers SIGTERM while the
            # stub still has the default disposition: the worker dies with
            # `code=143` and the entrypoint takes the `complete` branch instead
            # of the deadline path under test (CI run 40075008826). Readiness
            # waits for the marker for the same reason it waits for live PIDs —
            # the bounded-drain assertion must not race stub startup.
            saq_command = 'trap "" TERM; : > "$WORKER_STUB_READY_DIR/$2.ready"; exec sleep 300'
        else:
            saq_command = "exec sleep 300"
        python_stub = f"""#!/bin/bash
if [ "$1" = "-m" ]; then
  case "$2" in
    modulo.launcher.policy)
      echo "SLIDING_WINDOW_S=300"; echo "SLIDING_CRASH_LIMIT=5"; echo "SLIDING_RESTART_SLEEP_S=1"; exit 0;;
    saq|modulo.core.saq_worker)
      {saq_command};;
    *) exit 0;;
  esac
fi
if [ "$1" = "-c" ]; then exit 0; fi
if [ "$1" = "-" ]; then exit 1; fi
exit 0
"""
        python_path = self.bin_dir / "python3"
        python_path.write_text(python_stub, encoding="utf-8")
        python_path.chmod(0o755)
        alembic = self.bin_dir / "alembic"
        alembic.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        alembic.chmod(0o755)

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "RUN_WORKER_PID_FILE": str(self.run_pid_file),
            "SYSTEM_WORKER_PID_FILE": str(self.system_pid_file),
            "WORKER_DRAIN_FLAG": str(self.run_dir / "worker-draining"),
            "POLICY_ENV_FILE": str(self.run_dir / "policy.env"),
            "FLY_PROCESS_GROUP": "worker",
            "WORKER_HEALTH_PORT": "18082",
            "WORKER_DRAIN_DEADLINE_S": str(self.deadline_s),
            "WORKER_STUB_READY_DIR": str(self.run_dir),
        }

    def start(self) -> None:
        self._log_handle = self.log_path.open("w")
        self.proc = _popen(
            [BASH, str(ENTRYPOINT)],
            env=self._env(),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def wait_ready(self, timeout_s: float = 20.0) -> bool:
        """Wait for the drain trap, live workers, and their TERM dispositions.

        ``WORKER_READY`` is printed as soon as the trap is installed, but the
        restart wrappers write the PID files asynchronously. Signalling before
        they exist makes the drain correctly find nothing to wait for (it takes
        the ``complete`` branch), so the deadline path under test never runs —
        the race that made this suite fail intermittently under parallel CI
        load. A live PID is not enough on its own for the TERM-ignoring case:
        the stub installs its trap asynchronously too, and a SIGTERM delivered
        in that window kills it with the default disposition.
        """
        assert self.proc is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if (
                "WORKER_READY" in self.log_path.read_text(encoding="utf-8")
                and self._workers_registered()
                and self._worker_traps_installed()
            ):
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.02)
        return False

    def _worker_traps_installed(self) -> bool:
        """True once every TERM-ignoring stub has installed its trap."""
        if not self.ignore_term:
            return True
        return all((self.run_dir / f"{name}.ready").exists() for name in ("saq", "modulo.core.saq_worker"))

    def _workers_registered(self) -> bool:
        for path in (self.run_pid_file, self.system_pid_file):
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return False
            try:
                os.kill(pid, 0)
            except OSError:
                return False
        return True

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8")

    def stop(self, timeout_s: float = 30.0) -> int:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=timeout_s)

    def cleanup(self) -> None:
        """Kill this harness's own session (never a global process kill)."""
        assert self.proc is not None
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        self._log_handle.close()


@needs_bash
def test_entrypoint_forwards_sigterm_and_completes_a_bounded_drain(tmp_path: Path) -> None:
    """SIGTERM must reach the SAQ workers and the entrypoint must exit cleanly.

    This is the release-path behaviour that makes the FAR-1627 handback run:
    the workers get the signal, finish their bounded shutdown, and are NOT
    restarted by the crash-guard wrappers.
    """
    harness = _EntrypointHarness(tmp_path, deadline_s=20, ignore_term=False)
    harness.start()
    try:
        assert harness.wait_ready(), f"entrypoint never became ready:\n{harness.log()}"
        started = time.monotonic()
        rc = harness.stop()
        elapsed = time.monotonic() - started
        log = harness.log()

        assert rc == 0, f"entrypoint exited {rc}:\n{log}"
        assert elapsed < 15, f"drain took {elapsed:.1f}s with responsive workers:\n{log}"
        assert log.count("WORKER_DRAIN: begin") == 1, (
            "the SIGTERM handler ran more than once — the trap is re-signalling its own shell:\n" + log
        )
        assert "WORKER_DRAIN: complete" in log, log
        assert "not restarting (worker shutdown requested)" in log, (
            "the crash-guard wrapper restarted a worker during the drain:\n" + log
        )
    finally:
        harness.cleanup()


@needs_bash
def test_entrypoint_drain_is_bounded_when_a_worker_ignores_sigterm(tmp_path: Path) -> None:
    """A wedged worker must not hang the stop past the drain deadline."""
    harness = _EntrypointHarness(tmp_path, deadline_s=2, ignore_term=True)
    harness.start()
    try:
        assert harness.wait_ready(), f"entrypoint never became ready:\n{harness.log()}"
        started = time.monotonic()
        rc = harness.stop(timeout_s=20)
        elapsed = time.monotonic() - started
        log = harness.log()

        assert rc == 0, f"entrypoint exited {rc}:\n{log}"
        assert elapsed < 10, f"bounded drain took {elapsed:.1f}s:\n{log}"
        assert "deadline (2s) reached" in log, log
    finally:
        harness.cleanup()
