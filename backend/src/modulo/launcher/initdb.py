"""initdb orchestration for the bundled PostgreSQL (ADR 031 Decision 8).

Rules enforced here, all locked by tests:

* FIXED ``-U`` bootstrap role name (``modulo``) — NEVER the OS username.
* Locale pinned to ``C.UTF-8`` (TODO(P3): Windows initdb has no C.UTF-8
  locale; the P3 delivery must pick the Windows-appropriate locale or ship
  one — this module refuses loudly rather than guessing).
* initdb runs into a fresh temp dir inside the data-dir parent and the
  result is moved into place with an atomic rename that FAILS if the target
  exists — a kill mid-bootstrap can therefore never leave a half-initialised
  ``pgdata`` behind. Stale temp dirs from a killed attempt are swept on the
  next run (single-launcher ownership; ADR 031 Decision 5).
* NEVER initdb over a non-empty directory: missing ``PG_VERSION`` plus
  non-empty content means a corrupt/interrupted cluster — refuse with a
  clear error, never re-init over it.
* Every bundled-binary invocation goes through :func:`_run_command` with
  fully pinned arguments: initdb pins --pgdata/--username/--locale (it has
  no endpoint to bind); the postmaster builder pins --pgdata/-h/-p and is
  consumed by the slice-2 serving step.

Deterministic test seam: setting ``MODULO_TEST_PAUSE_AT`` to a registered
gate name makes :func:`bootstrap_pgdata` print ``PAUSED:<gate>`` to stdout
and block reading stdin. Tests SIGKILL or resume the process from that exact
point — no timing-based kills.
"""

import os
import secrets as _secrets
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

BOOTSTRAP_USERNAME = "modulo"
INITDB_LOCALE = "C.UTF-8"
PG_VERSION_FILE = "PG_VERSION"

# Registered pause gates (consumed via the MODULO_TEST_PAUSE_AT env var).
GATE_INITDB_PRE_RENAME = "initdb_pre_rename"

_TMP_DIR_PREFIX = ".initdb-tmp-"
_PWFILE_PREFIX = ".initdb-pwfile-"

_PAUSE_ENV_VAR = "MODULO_TEST_PAUSE_AT"

# The launcher-owned public surface (bootstrap_pgdata + the argv builders are
# consumed by the slice-2 serving step; vulture's dead-code gate special-cases
# __all__).
__all__ = [
    "BOOTSTRAP_USERNAME",
    "GATE_INITDB_PRE_RENAME",
    "INITDB_LOCALE",
    "InitdbError",
    "assert_supported_platform",
    "bootstrap_pgdata",
    "initdb_argv",
    "postgres_server_argv",
    "validate_target_dir",
]


class InitdbError(RuntimeError):
    """Raised when the bundled-cluster bootstrap cannot proceed safely."""


def _binary(bin_dir: Path, name: str) -> str:
    """Resolve a bundled binary path (TODO(P3): Windows needs ``.exe`` + ACL work)."""
    if sys.platform == "win32":
        name = f"{name}.exe"
    return str(bin_dir / name)


def _run_command(argv: list[str]) -> None:
    """Execute a bundled binary. The single seam every invocation goes through.

    Kept module-level so tests (and the subprocess-based SIGKILL test
    driver) can substitute a recorder without executing real binaries.
    """
    subprocess.run(argv, check=True)  # noqa: S603 — argv is fully constructed here


def _pause_at(gate: str) -> None:
    """Deterministic mid-bootstrap pause for tests (never active in production).

    Active only when ``MODULO_TEST_PAUSE_AT`` equals *gate*. Prints a
    ``PAUSED:<gate>`` marker line and then blocks on stdin — the parent test
    blocks on readline (no timing), then either writes a line to resume or
    SIGKILLs the process at exactly this point.
    """
    if os.environ.get(_PAUSE_ENV_VAR) != gate:
        return
    print(f"PAUSED:{gate}", flush=True)  # noqa: T201 — the parent test reads this line
    sys.stdin.readline()


def validate_target_dir(pgdata: Path) -> None:
    """Refuse any target that is not a missing path or a truly empty dir.

    * missing → fresh install, proceed.
    * existing empty dir → proceed.
    * existing dir WITH ``PG_VERSION`` → already an initialised cluster;
      re-initdb over it is forbidden (ADR 031 Decision 8).
    * existing NON-EMPTY dir WITHOUT ``PG_VERSION`` → corrupt/interrupted
      content; refuse with a clear error instead of guessing.
    """
    if not pgdata.exists():
        return
    if not pgdata.is_dir():
        raise InitdbError(f"Refusing to initdb: {pgdata} exists and is not a directory")
    entries = [entry for entry in pgdata.iterdir() if not entry.name.startswith(".")]
    if not entries:
        return
    if (pgdata / PG_VERSION_FILE).exists():
        raise InitdbError(
            f"Refusing to initdb: {pgdata} already contains an initialised "
            f"cluster ({PG_VERSION_FILE} present). Upgrades go through the "
            "pg-upgrade helper; never re-initdb an existing cluster."
        )
    raise InitdbError(
        f"Refusing to initdb: {pgdata} is non-empty but has no {PG_VERSION_FILE} "
        "— corrupt or interrupted cluster state. Remove the directory manually "
        "after inspecting it; initdb will never overwrite it."
    )


def initdb_argv(tmp_pgdata: Path, bin_dir: Path, username: str, pwfile: Path) -> list[str]:
    """Build the pinned initdb invocation (every argument explicit)."""
    return [
        _binary(bin_dir, "initdb"),
        "--pgdata",
        str(tmp_pgdata),
        "--username",
        username,
        "--locale",
        INITDB_LOCALE,
        "--auth=scram-sha-256",
        "--pwfile",
        str(pwfile),
    ]


def postgres_server_argv(pgdata: Path, host: str, port: int, bin_dir: Path) -> list[str]:
    """Build the pinned postmaster invocation (consumed by the slice-2 serving step).

    Every bundled-binary invocation pins its endpoint arguments — the
    postmaster binds ONLY the launcher-owned host/port persisted in
    state.json, never defaults.
    """
    return [
        _binary(bin_dir, "postgres"),
        "--pgdata",
        str(pgdata),
        "--host",
        host,
        "--port",
        str(port),
    ]


def _sweep_stale_tmp_dirs(parent: Path) -> None:
    """Remove temp dirs left behind by a killed previous attempt.

    Safe under single-launcher ownership (the launcher holds the exclusive
    data-dir lock, ADR 031 Decision 5); a concurrent bootstrap would never
    share this parent dir.
    """
    for entry in parent.glob(f"{_TMP_DIR_PREFIX}*"):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def assert_supported_platform() -> None:
    """Refuse Windows until the P3 delivery (locale/ACL/binary seams).

    Extracted as a seam so logic tests can bypass it on any platform — the
    fake-runner tests validate orchestration logic, not the OS.
    """
    if sys.platform == "win32":
        # TODO(P3): Windows initdb (locale, ACLs, .exe resolution) lands with
        # the P3 delivery; refuse loudly instead of mis-initialising.
        raise InitdbError("Bundled Postgres bootstrap is not supported on Windows yet (TODO(P3))")


def bootstrap_pgdata(
    pgdata: Path,
    *,
    password: str,
    username: str = BOOTSTRAP_USERNAME,
    bin_dir: Path | None = None,
    pause_hook: Callable[[str], None] | None = None,
) -> Path:
    """Initialise a fresh bundled cluster at *pgdata*, atomically.

    Steps: validate the target → sweep stale temp dirs → initdb into a fresh
    temp dir → verify ``PG_VERSION`` → deterministic pause gate → atomic
    rename into place (fails if the target now exists). ``password`` feeds
    initdb's ``--pwfile`` (scram-sha-256, ADR 031 Decision 8 — credentials
    are generated by the caller from the launcher secrets file, never
    hardcoded here). The pwfile is removed afterwards.
    """
    assert_supported_platform()
    if not pgdata.parent.exists():
        pgdata.parent.mkdir(parents=True)
    validate_target_dir(pgdata)
    _sweep_stale_tmp_dirs(pgdata.parent)
    effective_bin_dir = bin_dir if bin_dir is not None else pgdata.parent
    tmp_pgdata = pgdata.parent / f"{_TMP_DIR_PREFIX}{_secrets.token_hex(8)}"
    tmp_pgdata.mkdir()
    pwfile = pgdata.parent / f"{_PWFILE_PREFIX}{_secrets.token_hex(8)}"
    fd = os.open(str(pwfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, f"{password}\n".encode())
    finally:
        os.close(fd)
    try:
        _run_command(initdb_argv(tmp_pgdata, effective_bin_dir, username, pwfile))
        if not (tmp_pgdata / PG_VERSION_FILE).exists():
            raise InitdbError(
                f"initdb reported success but {PG_VERSION_FILE} is missing in {tmp_pgdata} — refusing to promote"
            )
        if pause_hook is not None:
            pause_hook(GATE_INITDB_PRE_RENAME)
        else:
            _pause_at(GATE_INITDB_PRE_RENAME)
        if pgdata.exists():
            raise InitdbError(f"Refusing to promote the fresh cluster: {pgdata} appeared mid-bootstrap")
        tmp_pgdata.replace(pgdata)
    finally:
        pwfile.unlink(missing_ok=True)
        if tmp_pgdata.exists():
            # Uniform cleanup on every failure path (a successful rename
            # moved the dir, so the exists() check only trips on failure).
            shutil.rmtree(tmp_pgdata, ignore_errors=True)
    return pgdata
