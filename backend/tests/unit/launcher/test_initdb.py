"""Unit tests for bundled-postgres initdb orchestration (FAR-671).

Locks the ADR 031 Decision 8 rules: FIXED bootstrap role, C.UTF-8 locale,
temp-dir + atomic rename (fails if the target exists), the non-empty-dir
refusal (corrupt = missing PG_VERSION + non-empty), argument pinning, the
deterministic pause gate ordering, and the SIGKILL-mid-bootstrap behaviour
via a subprocess + the MODULO_TEST_PAUSE_AT seam (no timing-based kills).
"""

import os
import subprocess
import sys
from pathlib import Path
from subprocess import Popen

import pytest

import modulo.launcher.initdb as initdb_module
from modulo.launcher.initdb import (
    BOOTSTRAP_USERNAME,
    GATE_INITDB_PRE_RENAME,
    INITDB_LOCALE,
    InitdbError,
    assert_supported_platform,
    bootstrap_pgdata,
    initdb_argv,
    postgres_server_argv,
    validate_target_dir,
)


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logic tests run on any OS: bypass the Windows-refusal seam."""
    if sys.platform == "win32":
        monkeypatch.setattr(initdb_module, "assert_supported_platform", lambda: None)


def _touch(path: Path, name: str = "content.bin") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text("x")
    return target


def _fake_initdb_writes_pg_version(argv: list[str]) -> None:
    """A fake initdb run: the temp cluster materialises with PG_VERSION."""
    tmp_index = argv.index("--pgdata")
    Path(argv[tmp_index + 1], "PG_VERSION").write_text("16\n")


def test_validate_target_missing_is_fresh(tmp_path: Path) -> None:
    assert validate_target_dir(tmp_path / "pgdata") is None  # fresh install accepted


def test_validate_target_empty_dir_is_fresh(tmp_path: Path) -> None:
    target = tmp_path / "pgdata"
    target.mkdir()
    assert validate_target_dir(target) is None  # empty dir accepted


def test_validate_target_refuses_initialised_cluster(tmp_path: Path) -> None:
    target = tmp_path / "pgdata"
    target.mkdir()
    (target / "PG_VERSION").write_text("16\n")
    (target / "global").mkdir()
    with pytest.raises(InitdbError, match="already contains an initialised"):
        validate_target_dir(target)


def test_validate_target_refuses_corrupt_non_empty_dir(tmp_path: Path) -> None:
    target = tmp_path / "pgdata"
    _touch(target)
    with pytest.raises(InitdbError, match="corrupt or interrupted"):
        validate_target_dir(target)


def test_validate_target_refuses_plain_file(tmp_path: Path) -> None:
    target = tmp_path / "pgdata"
    target.write_text("not a dir")
    with pytest.raises(InitdbError, match="not a directory"):
        validate_target_dir(target)


def test_initdb_argv_pins_every_argument(tmp_path: Path) -> None:
    tmp_pgdata = tmp_path / "tmp"
    pwfile = tmp_path / "pw"
    argv = initdb_argv(tmp_pgdata, tmp_path / "bin", BOOTSTRAP_USERNAME, pwfile)
    assert Path(argv[0]).stem == "initdb"
    assert "--pgdata" in argv
    assert str(tmp_pgdata) in argv
    assert "--username" in argv
    assert BOOTSTRAP_USERNAME in argv
    assert "--locale" in argv
    assert INITDB_LOCALE in argv
    assert "--auth=scram-sha-256" in argv
    assert "--pwfile" in argv
    assert str(pwfile) in argv


def test_bootstrap_role_name_is_fixed_not_os_username() -> None:
    # ADR 031 Decision 8: the bundled cluster's bootstrap role is FIXED —
    # never derived from the OS user running the launcher.
    assert BOOTSTRAP_USERNAME == "modulo"


def test_server_argv_pins_endpoint_arguments(tmp_path: Path) -> None:
    argv = postgres_server_argv(tmp_path / "pgdata", "127.0.0.1", 15432, tmp_path / "bin")
    assert Path(argv[0]).stem == "postgres"
    assert "--pgdata" in argv
    assert "--host" in argv
    assert "127.0.0.1" in argv
    assert "--port" in argv
    assert "15432" in argv


def test_bootstrap_happy_path_records_pinned_invocation_and_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgdata = tmp_path / "pgdata"
    calls: list[list[str]] = []

    def _recorder(argv: list[str]) -> None:
        calls.append(argv)
        _fake_initdb_writes_pg_version(argv)

    monkeypatch.setattr(initdb_module, "_run_command", _recorder)
    gates: list[str] = []
    result = bootstrap_pgdata(pgdata, password="generated-pw", pause_hook=gates.append)

    assert result == pgdata
    assert pgdata.exists()
    assert (pgdata / "PG_VERSION").exists()
    assert len(calls) == 1
    argv = calls[0]
    assert "--username" in argv
    assert BOOTSTRAP_USERNAME in argv
    assert "--locale" in argv
    assert INITDB_LOCALE in argv
    assert "--pgdata" in argv
    # The pinned --pgdata pointed at the TEMP dir, and the temp dir no longer
    # exists (promoted by rename into pgdata).
    tmp_arg = argv[argv.index("--pgdata") + 1]
    assert tmp_arg != str(pgdata)
    assert Path(tmp_arg).exists() is False
    # The pause gate fired AFTER initdb and BEFORE the rename (pgdata only
    # exists now, so the rename came after the gate).
    assert gates == [GATE_INITDB_PRE_RENAME]
    # The pwfile was consumed and removed.
    leftovers = [entry for entry in tmp_path.iterdir() if entry.name.startswith(".initdb-pwfile-")]
    assert not leftovers


def test_bootstrap_refuses_non_empty_target_before_running_initdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgdata = tmp_path / "pgdata"
    _touch(pgdata)
    called: list[list[str]] = []
    monkeypatch.setattr(initdb_module, "_run_command", lambda argv: called.append(argv))
    with pytest.raises(InitdbError, match="corrupt or interrupted"):
        bootstrap_pgdata(pgdata, password="pw")
    assert not called  # initdb never ran over the corrupt target


def test_bootstrap_refuses_when_target_appears_mid_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgdata = tmp_path / "pgdata"
    monkeypatch.setattr(initdb_module, "_run_command", _fake_initdb_writes_pg_version)

    def _hook(_gate: str) -> None:
        # Simulate a concurrent actor creating the target before the rename.
        pgdata.mkdir()

    with pytest.raises(InitdbError, match="appeared mid-bootstrap"):
        bootstrap_pgdata(pgdata, password="pw", pause_hook=_hook)
    # The temp dir was cleaned; the concurrent dir is left alone (never
    # overwritten).
    leftovers = [entry for entry in tmp_path.iterdir() if entry.name.startswith(".initdb-tmp-")]
    assert not leftovers


def test_bootstrap_sweeps_stale_temp_dirs_from_killed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgdata = tmp_path / "pgdata"
    stale = tmp_path / ".initdb-tmp-deadbeef"
    _touch(stale, "PG_VERSION")
    monkeypatch.setattr(initdb_module, "_run_command", _fake_initdb_writes_pg_version)
    bootstrap_pgdata(pgdata, password="pw")
    assert stale.exists() is False
    assert (pgdata / "PG_VERSION").exists()


def test_bootstrap_refuses_promotion_when_pg_version_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A "successful" initdb that produced no PG_VERSION must not be promoted.
    pgdata = tmp_path / "pgdata"
    monkeypatch.setattr(initdb_module, "_run_command", lambda argv: None)
    with pytest.raises(InitdbError, match="PG_VERSION"):
        bootstrap_pgdata(pgdata, password="pw")


_DRIVER_SCRIPT = """
import sys
from pathlib import Path

import modulo.launcher.initdb as initdb

def _fake_run(argv):
    tmp_index = argv.index("--pgdata")
    Path(argv[tmp_index + 1], "PG_VERSION").write_text("16\\n")

initdb._run_command = _fake_run
initdb.bootstrap_pgdata(Path(sys.argv[1]), password="generated-pw")
print("DONE", flush=True)
"""


def _spawn_driver(pgdata: Path, pause_env: dict[str, str]) -> Popen[str]:
    env = dict(os.environ)
    env.update(pause_env)
    # The driver is bounded: the interactive phase reads the deterministic
    # PAUSED marker (printed before any blocking), and both consumers call
    # proc.wait(timeout=30). Imported as a bare name because subprocess.Popen
    # does not accept a construction-time timeout keyword.
    return Popen(  # noqa: S603 — fixed argv, no user input
        [sys.executable, "-c", _DRIVER_SCRIPT, str(pgdata)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="bundled-postgres bootstrap refuses Windows (TODO(P3))")
def test_sigkill_mid_bootstrap_leaves_no_partial_pgdata(tmp_path: Path) -> None:
    """SIGKILL at the deterministic gate leaves NO half-initialised cluster."""
    pgdata = tmp_path / "pgdata"
    proc = _spawn_driver(pgdata, {"MODULO_TEST_PAUSE_AT": GATE_INITDB_PRE_RENAME})
    try:
        marker = proc.stdout.readline().strip() if proc.stdout else ""
        assert marker == f"PAUSED:{GATE_INITDB_PRE_RENAME}"
        proc.kill()
        _ = proc.wait(timeout=30)
        assert pgdata.exists() is False
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    # Self-healing: a fresh bootstrap on the same data dir succeeds (stale
    # temp dirs swept) — in-process with a fake runner.
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(initdb_module, "_run_command", _fake_initdb_writes_pg_version)
        bootstrap_pgdata(pgdata, password="pw")
    finally:
        monkeypatch.undo()
    assert (pgdata / "PG_VERSION").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="bundled-postgres bootstrap refuses Windows (TODO(P3))")
def test_pause_gate_resume_completes_the_bootstrap(tmp_path: Path) -> None:
    """Resuming from the gate (stdin line) completes the atomic rename."""
    pgdata = tmp_path / "pgdata"
    proc = _spawn_driver(pgdata, {"MODULO_TEST_PAUSE_AT": GATE_INITDB_PRE_RENAME})
    try:
        marker = proc.stdout.readline().strip() if proc.stdout else ""
        assert marker == f"PAUSED:{GATE_INITDB_PRE_RENAME}"
        if proc.stdin:
            proc.stdin.write("go\n")
            proc.stdin.flush()
            proc.stdin.close()
        _ = proc.wait(timeout=30)
        assert pgdata.exists() is True
        assert (pgdata / "PG_VERSION").exists()
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the refusal seam is Windows-specific")
def test_bootstrap_refuses_windows_today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The autouse fixture bypasses the platform guard for logic tests; this
    # test re-installs the REAL guard to prove the refusal itself.
    monkeypatch.setattr(initdb_module, "assert_supported_platform", assert_supported_platform)
    with pytest.raises(InitdbError, match=r"TODO\(P3\)"):
        bootstrap_pgdata(tmp_path / "pgdata", password="pw")
