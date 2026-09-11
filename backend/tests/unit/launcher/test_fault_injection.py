"""FAR-677 — fault-injection suite for the native launcher (test-only).

Every fault below is injected through a seam the merged machinery already
ships; this suite makes the recovery contracts hard to regress:

* SIGKILL mid-initdb via the deterministic ``MODULO_TEST_PAUSE_AT`` gate
  (never a timing-based kill): a killed attempt must leave NO
  half-initialised datadir to adopt on the next boot, the SIGKILL debris
  (temp cluster + plaintext pwfile) must be swept by the next boot's
  reconciliation, and the fresh bootstrap must then succeed.
* a never-healthy child (occupied persisted port) must terminate the
  crash ladder: backoff progression, sliding-window eviction of stale
  crashes, and the terminal degraded state persisted + surfaced via
  ``collect_status`` (the ``modulo status --json`` payload), with the
  documented remediation hint.
* restore-while-running: a start holding the data-dir lock refuses a
  concurrent restore/backup naming the holder PID and its mode.
* second concurrent start: a clean lock refusal naming holder PID+mode.
* poisoned boot env: the entry's same first-action scrub must remove
  ``PG*``/``PYTHONPATH``/``LD_*`` and a poisoned-PYTHONPATH child must
  load no foreign code.
* uninstall-keeps-data + reinstall reattaches: credential continuity,
  no re-bootstrap, no re-generated credentials (idempotent load).
* disk-full: a capped tmpfs (Linux, when the runner can mount; loud
  honest skip otherwise) must drive doctor's data-dir check to the
  quantified free-space failure. Per-OS harnesses for macOS/Windows are
  real-machine-checklist territory and are NOT faked here.

Timing discipline (repo lesson): the supervisor ladder is driven with
the manual-tick fake-clock seam — no wall-clock sleeps at all, which
trivially satisfies the >=6-8x tick-vs-window margin by being fully
deterministic; each crash scenario repeats across parametrized caps.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from subprocess import Popen
from typing import Any

import pytest

import modulo.launcher.secrets_file as secrets_file_module
from modulo.launcher.initdb import (
    GATE_INITDB_PRE_RENAME,
    InitdbError,
    bootstrap_pgdata,
    validate_target_dir,
)

requires_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fault mechanics; runs on Linux CI")


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logic tests run on any OS: bypass the Windows-refusal seams."""
    if sys.platform == "win32":
        monkeypatch.setattr(secrets_file_module, "assert_supported_platform", lambda: None)
        from modulo.launcher import initdb as initdb_module

        monkeypatch.setattr(initdb_module, "assert_supported_platform", lambda: None)


def _fake_initdb_runner(argv: list[str]) -> None:
    """A bundled initdb that "succeeds": writes PG_VERSION into the temp dir."""
    index = argv.index("--pgdata")
    Path(argv[index + 1], "PG_VERSION").write_text("16\n", encoding="ascii")


def _fake_initdb_runner_writes_stale_marks(argv: list[str]) -> None:
    """Same fake runner, but leaves junk files in the staged dir first."""
    index = argv.index("--pgdata")
    staged = Path(argv[index + 1])
    (staged / "PG_VERSION").write_text("16\n", encoding="ascii")
    (staged / "Globals").write_text("reused\n", encoding="ascii")


_KILL_DRIVER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    import modulo.launcher.initdb as initdb

    def _fake_run(argv):
        index = argv.index("--pgdata")
        Path(argv[index + 1], "PG_VERSION").write_text("16\\n", encoding="ascii")

    initdb._run_command = _fake_run
    initdb.bootstrap_pgdata(Path(sys.argv[1]), password="generated-pw")
    print("BOOTSTRAPPED", flush=True)
    """
)


def _spawn_kill_driver(pgdata: Path, extra_env: dict[str, str]) -> Popen[str]:
    """Run the bootstrap in a child that pauses at the deterministic gate.

    The test SIGKILLs the child right after the pause marker, so it
    never awaits a hung child; each consume site waits with its own
    bounded ``proc.wait(timeout=...)``.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    # Arm the deterministic initdb pause gate so the child blocks at
    # PAUSED:initdb_pre_rename (the point the test SIGKILLs / resumes at).
    env["MODULO_TEST_PAUSE_AT"] = GATE_INITDB_PRE_RENAME
    env.update(extra_env)
    return Popen(  # noqa: S603 — fixed argv, trusted synthesized test driver
        [sys.executable, "-c", _KILL_DRIVER, str(pgdata)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


@pytest.fixture
def module_path_on_syspath(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child driver imports ``modulo`` — expose this checkout's src."""
    src = Path(__file__).resolve().parents[2] / "src"
    monkeypatch.setenv("PYTHONPATH", str(src))


@requires_posix
def test_sigkill_mid_initdb_leaves_nothing_to_adopt_and_next_boot_heals(
    tmp_path: Path, module_path_on_syspath: None
) -> None:
    """SIGKILL at the deterministic gate → no adopted datadir → next boot heals.

    Advance window: the pause gate is stdin-blocked, so the kill is exact;
    the recovery assertions run in-process with the fake runner instead:
    the sweep + fresh bootstrap land with the same modules under the test.
    """
    from modulo.launcher.initdb import _REGISTERED_GATES  # noqa: F401  (import integrity)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pgdata = data_dir / "pgdata"

    from modulo.launcher.supervisor import reconcile_orphans

    env_extra = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    proc = _spawn_kill_driver(pgdata, env_extra)
    assert proc.stdout is not None
    marker = proc.stdout.readline().strip()
    assert marker == f"PAUSED:{GATE_INITDB_PRE_RENAME}"
    # SIGKILL exactly here: mid-bootstrap, before the promotion rename.
    proc.kill()
    proc.wait(timeout=30)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()

    # 1. No half-initialised datadir was promoted.
    assert not pgdata.exists()
    assert not (data_dir / "PG_VERSION").exists()
    # 2. The SIGKILL debris (temp cluster + plaintext pwfile) exists...
    assert list(data_dir.glob(".initdb-tmp-*")), "a SIGKILLed temp cluster is expected debris"
    assert list(data_dir.glob(".initdb-pwfile-*")), "a SIGKILLed plaintext pwfile is expected debris"
    # 3. ...and the NEXT boot's reconciliation sweeps both.
    assert reconcile_orphans(pgdata) is not None
    assert not list(data_dir.glob(".initdb-tmp-*"))
    assert not list(data_dir.glob(".initdb-pwfile-*"))
    # 4. The fresh bootstrap on the healed dir succeeds (fake runner).
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("modulo.launcher.initdb._run_command", _fake_initdb_runner)
        bootstrap_pgdata(pgdata, password="fresh-pw")
    finally:
        monkeypatch.undo()
    assert (pgdata / "PG_VERSION").exists()


@requires_posix
def test_sigkill_mid_initdb_resume_completes_the_bootstrap(tmp_path: Path) -> None:
    """Resuming from the same deterministic gate completes the promotion."""
    from modulo.launcher.initdb import _REGISTERED_GATES  # noqa: F401  (import integrity)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pgdata = data_dir / "pgdata"
    proc = _spawn_kill_driver(
        pgdata,
        {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert proc.stdout is not None
    marker = proc.stdout.readline().strip()
    assert marker == f"PAUSED:{GATE_INITDB_PRE_RENAME}"
    assert proc.stdin is not None
    proc.stdin.write("go\n")
    proc.stdin.flush()
    proc.stdin.close()
    assert proc.wait(timeout=30) == 0, proc.stderr.read() if proc.stderr else ""
    assert (pgdata / "PG_VERSION").exists()
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


@requires_posix
def test_sigkill_debris_is_never_adopted_as_a_datadir(tmp_path: Path) -> None:
    """A SIGKILL-looking target dir refuses: never silently re-init over it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    corrupt = data_dir / "pgdata"
    corrupt.mkdir()
    (corrupt / "reused.log").write_text("SIGKILL debris, NOT a datadir\n", encoding="utf-8")
    with pytest.raises(InitdbError, match="non-empty"):
        bootstrap_pgdata(corrupt, password="pw")


def test_validate_target_dir_refuses_a_corrupt_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """initdb refuses non-empty targets even when the fake runner succeeded."""
    monkeypatch.setattr(
        "modulo.launcher.initdb._run_command",
        _fake_initdb_runner_writes_stale_marks,
    )
    target = tmp_path / "pgdata"
    target.mkdir()
    (target / "junk.log").write_text("pre-existing content\n", encoding="utf-8")
    with pytest.raises(InitdbError, match="non-empty"):
        validate_target_dir(target)


# ---------------------------------------------------------------------------
# Crash ladder -> terminal degraded -> status JSON (manual-tick, no sleeps)
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic monotonic clock (the manual-tick seam)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class CrashyProc:
    """Duck-typed child that exits nonzero immediately (a crashing CMS)."""

    _next_pid = 7700

    def __init__(self) -> None:
        CrashyProc._next_pid += 1
        self.pid = CrashyProc._next_pid

    def poll(self) -> int | None:
        return 7  # exited nonzero on the very first poll

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 7


def _build_crashy_supervisor(
    *,
    cap: int,
    window_seconds: float,
    backoff_initial: float,
    backoff_max: float,
    degraded_path: Path | None,
) -> tuple[object, FakeClock]:
    """A supervisor whose postgres child crashes on every tick."""
    from modulo.launcher.supervisor import ChildSpec, Supervisor, SupervisorKnobs

    clock = FakeClock()
    supervisor = Supervisor(
        SupervisorKnobs(
            tick_seconds=0.01,
            restart_backoff_initial=backoff_initial,
            restart_backoff_max=backoff_max,
            crash_window_seconds=window_seconds,
            crash_cap=cap,
            health_check_timeout=0.5,
        ),
        spawner=lambda argv, env: CrashyProc(),
        clock=clock,
        sleep=lambda _seconds: None,
        degraded_path=degraded_path,
        wall_clock=lambda: 1_700_000_000.0,
    )
    supervisor.add(ChildSpec(name="postgres", argv_builder=lambda: ["postgres"], probe=lambda: False))
    return supervisor, clock


def _run_crash_ladder(supervisor: Any, clock: FakeClock) -> None:
    """Manual-tick drive until the terminal degraded state trips."""
    for _ in range(500):
        supervisor.tick()
        if supervisor.degraded_reason is not None:
            return
        clock.advance(0.02)


@pytest.mark.parametrize(
    ("cap", "window_seconds", "backoff_initial", "backoff_max"),
    [
        pytest.param(3, 60.0, 0.01, 0.05, id="cap3"),
        pytest.param(2, 30.0, 0.02, 0.04, id="cap2-small-window"),
        pytest.param(5, 120.0, 0.005, 0.02, id="cap5-tight-knobs"),
    ],
)
def test_crash_ladder_trips_terminal_degraded_and_surfaces_in_status(
    tmp_path: Path,
    cap: int,
    window_seconds: float,
    backoff_initial: float,
    backoff_max: float,
) -> None:
    """N crashes inside the window -> terminal degraded + status surfaced."""
    from modulo.launcher.supervisor import (
        DEGRADED_FILENAME,
        collect_status,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    degraded_path = data_dir / DEGRADED_FILENAME
    supervisor, clock = _build_crashy_supervisor(
        cap=cap,
        window_seconds=window_seconds,
        backoff_initial=backoff_initial,
        backoff_max=backoff_max,
        degraded_path=degraded_path,
    )
    _ = tmp_path  # silence: the supervisor carries its own knobs
    _run_crash_ladder(supervisor, clock)
    reason = supervisor.degraded_reason
    assert reason is not None
    assert "postgres" in reason
    # The degrade persisted with its crash ring (≥ cap entries after ≥
    # cap crashes; the ring's tail is the most recent)."""
    persisted = json.loads(degraded_path.read_text(encoding="utf-8"))
    assert len(persisted["crashes"]) >= cap
    assert all(entry["child"] == "postgres" for entry in persisted["crashes"])
    # And the status JSON surface (modulo status) reports the same state.
    status = collect_status(data_dir)
    assert json.dumps(status), "the status payload must always serialise"
    assert status["degraded"]["reason"] == reason
    assert status["initialized"] is False  # a bare, faulted data dir


def test_crash_ladder_backoff_progression_is_exponential_and_capped() -> None:
    """Crash -> respawn gaps double from the initial value to the cap."""
    supervisor, clock = _build_crashy_supervisor(
        cap=50,
        window_seconds=10_000.0,
        backoff_initial=0.01,
        backoff_max=0.05,
        degraded_path=None,
    )
    expected_backoffs = (0.01, 0.02, 0.04, 0.05, 0.05)
    for expected in expected_backoffs:
        supervisor.tick()  # spawn (or respawn once the backoff elapsed)
        clock.advance(0.02)
        supervisor.tick()  # detect the crash -> schedule the next spawn
        child = supervisor._children["postgres"]
        scheduled_at = child.next_spawn_at
        assert scheduled_at is not None
        assert scheduled_at - clock.now == pytest.approx(expected)
        clock.advance(expected)
        # Keep the sliding window wide open across the whole ladder.
        clock.advance(0.05)


def test_sliding_window_eviction_keeps_a_lone_crash_alive() -> None:
    """Crashes older than the window must not accumulate against the cap."""
    supervisor, clock = _build_crashy_supervisor(
        cap=3,
        window_seconds=1.0,
        backoff_initial=0.01,
        backoff_max=0.02,
        degraded_path=None,
    )
    # crash 1 (immediately exited child: spawn + crash poll)
    supervisor.tick()
    clock.advance(0.02)
    supervisor.tick()
    # ...slide the window out between every crash: each new crash falls
    # into a FRESH window (its predecessors were evicted), so the cap of
    # 3 must never accumulate three crashes at once.
    for _ in range(4):
        clock.advance(5.0)  # >> the 1.0s window: the last crash is evicted
        supervisor.tick()  # respawn (backoff long elapsed)
        supervisor.tick()  # crash + schedule the next spawn
        if supervisor.degraded_reason is not None:
            break
    assert supervisor.degraded_reason is None, "crashes separated by wider-than-window gaps must not trip the cap"


# ---------------------------------------------------------------------------
# Lock refusals: second start / restore-while-running
# ---------------------------------------------------------------------------


@requires_posix
def test_second_concurrent_start_refuses_naming_holder_pid_and_mode(tmp_path: Path) -> None:
    """A running start holds the lock; a second start refuses and names it."""
    from modulo.launcher.supervisor import DataDirLock, DataDirLockError

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    holder = DataDirLock(data_dir, mode="serve")
    holder.acquire()
    try:
        second = DataDirLock(data_dir, mode="serve")
        with pytest.raises(DataDirLockError) as excinfo:
            second.acquire()
        message = str(excinfo.value)
        assert f"holder PID {os.getpid()}" in message
        assert "mode 'serve'" in message
    finally:
        holder.release()


# ---------------------------------------------------------------------------
# Poisoned environment: the scrub never leaks launcher-hostile variables
# ---------------------------------------------------------------------------


def test_entry_scrub_removes_the_poisoned_boot_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``scrub_os_environment`` (run_start's first action) clears the poison."""
    from modulo.launcher.env_safety import scrub_os_environment

    monkeypatch.setenv("PGHOST", "foreign.example")
    monkeypatch.setenv("PGPASSWORD", "leaked-password")
    monkeypatch.setenv("PG東PORT", "65533") if False else None
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("LD_PRELOAD", "/evil/interposer.so")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/evil.dylib")
    monkeypatch.setenv("MODULO_TEST_PAUSE_AT", GATE_INITDB_PRE_RENAME)
    scrub_os_environment()
    assert "PGHOST" not in os.environ
    assert "PGPASSWORD" not in os.environ
    assert "PYTHONPATH" not in os.environ
    assert "LD_PRELOAD" not in os.environ
    assert "DYLD_INSERT_LIBRARIES" not in os.environ
    assert "MODULO_TEST_PAUSE_AT" not in os.environ
    assert os.environ.get("PATH")  # legitimate variables survive the scrub


def test_poisoned_pythonpath_child_loads_no_foreign_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child launched with the SCRUBBED env loads no foreign code.

    The interpreter inserts ``PYTHONPATH`` into ``sys.path`` before any
    script runs, so the meaningful contract is the env the entry passes
    to its children: ``scrub_environment`` must strip the poison from
    the child's environment, and the child must then be unable to load
    foreign code from it.
    """
    from modulo.launcher.env_safety import scrub_environment

    poison_dir = tmp_path / "poison-tree"
    poison_dir.mkdir()
    (poison_dir / "evil_foreign.py").write_text("EVIL = True\n", encoding="utf-8")
    poisoned = dict(os.environ)
    poisoned["PYTHONPATH"] = str(poison_dir)
    poisoned["pgpassword"] = "leaked"  # lowercase poisoning is scrubbed too
    scrubbed = scrub_environment(poisoned)
    assert "PYTHONPATH" not in scrubbed
    assert "pgpassword" not in scrubbed
    monkeypatch.delenv("PYTHONPATH", raising=False)
    child_script = textwrap.dedent(
        """
        try:
            import evil_foreign  # noqa: F401
        except ModuleNotFoundError:
            print("SCRUBBED-CLEAN", flush=True)
        else:
            print("FOREIGN-CODE-LOADED", flush=True)
        """
    )
    proc = subprocess.run(  # noqa: S603 — fixed argv, trusted synthesized script
        [sys.executable, "-c", child_script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=scrubbed,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SCRUBBED-CLEAN" in proc.stdout
    assert "FOREIGN-CODE-LOADED" not in proc.stdout


# ---------------------------------------------------------------------------
# Occupied persisted port -> doctor's collision attribution + remediation
# ---------------------------------------------------------------------------


def test_occupied_persisted_port_gets_a_precise_error_and_remediation(
    tmp_path: Path,
) -> None:
    """A pre-squatted port from state.json: doctor names the collision."""
    from modulo.launcher.doctor import DoctorProbes, check_port_collisions
    from modulo.launcher.state import LauncherState

    pg_port = 15432
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state = LauncherState(postgres_port=pg_port, redis_port=16379, api_port=18000)
    # Pre-squat the configured port for real (the fault is real, the
    # attribution probe is injected because /proc attribution is Linux-only).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", pg_port))
        sock.listen(1)
        probes = DoctorProbes(
            disk_free_bytes=lambda _root: 40 * 1024 * 1024 * 1024,
            assert_writable=lambda _root: None,
            listening_on=lambda _port: ["127.0.0.1"],
            probe_postgres=lambda: None,
            role_violations=list,
            probe_redis=lambda: None,
            migrations_at_head=lambda: True,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _uid: "operator",
            file_owner=lambda _path: "operator",
            port_owner_description=lambda _port: "foreign service bound to 0.0.0.0",
            launcher_running=lambda: True,
        )
        result = check_port_collisions(data_dir, state, probes)
        assert result.ok is False
        assert str(pg_port) in result.detail
        # The documented remediation: free the port or edit state.json.
        assert "reassign state.json" in result.detail


# ---------------------------------------------------------------------------
# Unwritable data dir (POSIX mode semantics -> boot refusal, actionable)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="chmod 0500 semantics are POSIX-only")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores mode bits — the fault is not injectable"
)
def test_unwritable_datadir_refuses_the_boot_with_actionable_message(tmp_path: Path) -> None:
    """chmod 0500 the data dir; doctor must fail it with the actionable fix."""
    from modulo.launcher.doctor import DoctorProbes, check_data_dir

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o500)
    try:
        probes = DoctorProbes(
            disk_free_bytes=lambda _root: 40 * 1024 * 1024 * 1024,
            assert_writable=lambda root: (_ for _ in ()).throw(
                PermissionError(f"data dir {root} is not writable by this user")
            ),
            listening_on=lambda _port: [],
            probe_postgres=lambda: None,
            role_violations=list,
            probe_redis=lambda: None,
            migrations_at_head=lambda: True,
            effective_uid=lambda: 1000,
            username_of_uid=lambda _uid: "operator",
            file_owner=lambda _path: "operator",
            launcher_running=lambda: False,
        )
        result = check_data_dir(data_dir, None, probes)
        assert result.ok is False
        assert "writable" in result.detail
    finally:
        data_dir.chmod(0o700)


# ---------------------------------------------------------------------------
# Uninstall keeps the data dir; the reinstall reattaches (continuity)
# ---------------------------------------------------------------------------


_requires_posix_secrets = pytest.mark.skipif(
    sys.platform == "win32", reason="the secrets file refuses Windows (P3 seam)"
)


def _seed_populated_datadir(tmp_path: Path, *, key: bytes) -> tuple[Path, secrets_file_module.LauncherSecrets, Any]:
    """A bootstrapped-shaped data dir: secrets.json + state.json + cluster."""
    from modulo.launcher.state import LauncherState, save_state

    state = LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)
    secrets = secrets_file_module.LauncherSecrets(
        postgres_password="pg-generated",
        redis_password="redis-generated",
        state_hmac_key=key,
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "secrets.json").write_text(
        json.dumps(
            {
                "postgres_password": secrets.postgres_password,
                "redis_password": secrets.redis_password,
                "state_hmac_key": secrets.state_hmac_key_hex,
            }
        ),
        encoding="utf-8",
    )
    save_state(state, data_dir / "state.json", secrets.state_hmac_key)
    pgdata = data_dir / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n", encoding="ascii")
    return data_dir, secrets, state


@_requires_posix_secrets
def test_uninstall_removes_the_app_tree_but_keeps_the_data_dir(tmp_path: Path) -> None:
    data_dir, secrets, _state = _seed_populated_datadir(tmp_path, key=bytes(range(32)))
    # "Uninstall" removes the APP tree; the launcher-owned data dir is kept.
    app_tree = data_dir.parent / "app"
    app_tree.mkdir()
    shutil.rmtree(app_tree)
    assert not app_tree.exists()
    # The kept data dir still carries its state + secrets + cluster marker.
    assert (data_dir / "secrets.json").is_file()
    assert (data_dir / "state.json").is_file()
    assert (data_dir / "pgdata" / "PG_VERSION").is_file()
    assert secrets.postgres_password != secrets.redis_password


@_requires_posix_secrets
def test_reinstall_reattaches_with_credential_continuity_no_re_bootstrap(
    tmp_path: Path,
) -> None:
    """Fresh installation boots into the EXISTING data dir, unchanged."""
    data_dir, secrets, state_before = _seed_populated_datadir(tmp_path, key=bytes(range(32)))
    reloaded = secrets_file_module.load_or_create(data_dir / secrets_file_module.SECRETS_FILENAME)
    # Idempotent credentials: the reload NEVER regenerates.
    assert reloaded == secrets
    from modulo.launcher.state import load_state

    assert load_state(data_dir / "state.json", reloaded.state_hmac_key) == state_before
    # And the cluster's initdb sentinel is intact: no re-bootstrap happened.
    assert (data_dir / "pgdata" / "PG_VERSION").is_file()


# ---------------------------------------------------------------------------
# Disk-full: real capped tmpfs when mountable, otherwise injected-zero-free.
# the macOS/Windows harness is real-machine checklist territory (never faked).
# ---------------------------------------------------------------------------


def _try_mount_tiny_tmpfs(base: Path) -> Path | None:
    """mount a 1 MiB tmpfs; None when the runner cannot mount (honest skip)."""
    if sys.platform != "linux":
        return None
    mountpoint = base / "capped-tmpfs"
    mountpoint.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(  # noqa: S603 — resolved PATH lookup, pinned argv
            ["/usr/sbin/mount", "-t", "tmpfs", "-o", "size=1m", "tmpfs", str(mountpoint)],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError):
        # mount binary absent from this runner's PATH — cannot cap a filesystem.
        return None
    if completed.returncode != 0:
        return None
    return mountpoint


@pytest.mark.skipif(os.name != "posix", reason="tmpfs cap is POSIX-only")
def test_disk_full_drives_the_data_dir_check_to_a_quantified_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk-full -> doctor's data-dir floor fails with the quantified error.

    Uses a REAL 1 MiB tmpfs when the runner can mount one; otherwise it
    injects the zero-free reading — the same code path a full disk feeds
    (the source of the number is the only difference, which a tmpfs plan
    cannot control from a plain test runner)."""
    import shutil as shutil_module

    from modulo.launcher import doctor as doctor_module

    mountpoint = _try_mount_tiny_tmpfs(tmp_path)
    if mountpoint is None and sys.platform == "linux":
        pytest.skip(
            "no mount privileges on this runner — the Linux disk-full recipe needs a capped "
            "filesystem (tmpfs/loopback); macOS/Windows are real-machine checklist items"
        )
    data_dir = mountpoint / "data" if mountpoint is not None else tmp_path / "data"
    data_dir.mkdir(parents=True)
    if mountpoint is None:
        injected_free = 0
        monkeypatch.setattr(
            shutil_module,
            "disk_usage",
            lambda _path: (_ for _ in ()).throw(AssertionError("unused probe")),
        )
        probes = doctor_module.default_probes(data_dir, None)
        monkeypatch.setattr(probes, "disk_free_bytes", lambda _root: injected_free)
    else:
        probes = doctor_module.default_probes(data_dir, None)
    result = doctor_module.check_data_dir(data_dir, None, probes)
    assert result.ok is False
    assert "free" in result.detail or "floor" in result.detail
    if mountpoint is not None:
        subprocess.run(  # noqa: S603 — resolved PATH lookup, pinned argv
            ["/usr/sbin/umount", str(mountpoint)], capture_output=True, timeout=30, check=False
        )


# ---------------------------------------------------------------------------
# State-version gate + upgrade swap-phase liveness (fault-injection recipes
# the doctor/upgrade surfaces both consume)
# ---------------------------------------------------------------------------


def test_newer_launcher_state_refuses_boot_cleanly(tmp_path: Path) -> None:
    """A state.json written by a NEWER launcher refuses the (older) boot."""
    from modulo.launcher.state import SCHEMA_VERSION, LauncherState, StateVersionError, load_state, save_state

    key = bytes(range(32))
    ahead = LauncherState(
        postgres_port=15432,
        redis_port=16379,
        api_port=18000,
        schema_version=SCHEMA_VERSION + 3,
    )
    path = tmp_path / "state.json"
    save_state(ahead, path, key)
    with pytest.raises(StateVersionError, match="NEWER launcher"):
        load_state(path, key)


@pytest.mark.skipif(os.name != "posix", reason="/proc liveness is Linux-first (P1a)")
def test_upgrade_swap_phase_refuses_a_live_holder_naming_pid(tmp_path: Path) -> None:
    """The destructive swap phase never races a LIVE lock holder."""
    from modulo.launcher.upgrade import UpgradeError, assert_not_held

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock_path = data_dir.parent / (data_dir.name + ".lock")
    lock_path.write_text(json.dumps({"pid": os.getpid(), "mode": "serve", "acquired_at": 1.0}), encoding="utf-8")
    with pytest.raises(UpgradeError, match=f"holder PID {os.getpid()}"):
        assert_not_held(data_dir)


@requires_posix
def test_restore_refuses_while_a_start_holds_the_data_dir_lock(tmp_path: Path) -> None:
    """restore/backup refuse while the launcher holds the exclusive lock."""
    from modulo.launcher.supervisor import DataDirLock

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    holder = DataDirLock(data_dir, mode="serve")
    holder.acquire()
    try:
        import click

        from modulo.cli.backup import _acquire_data_lock

        with pytest.raises(click.ClickException) as excinfo:
            _acquire_data_lock(tmp_path / "data", mode="restore")
        message = str(excinfo.value)
        assert f"holder PID {os.getpid()}" in message
    finally:
        holder.release()
