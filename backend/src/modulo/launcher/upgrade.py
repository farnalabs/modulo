"""Installer-enforced pre-upgrade pg_dump orchestration (FAR-672 / ADR 031 Decision 7).

P1a upgrade flow = rerun the installer. The installer ENFORCES a pre-upgrade
pg_dump before it touches anything version-specific: it calls the OLD
installation's bundled runtime here (``python -m modulo.launcher.upgrade``),
which resolves the BUNDLED pg_dump client, dumps the bundled app database to
a versioned snapshot directory inside the data dir, verifies the dump is
non-empty, and prints the snapshot path as its final stdout line.

On ANY dump failure the helper raises :class:`UpgradeError` and the installer
ABORTS — no binary swap happens without a verified snapshot. ``--skip-backup``
in install.sh is the operator's loud, explicit escape hatch for the SECOND
installer run (the dump already succeeded; the stack must stay stopped for
the swap — see the install.sh comment block).

The bundled stack must be REACHABLE while dumping (pg_dump talks to a live
Postgres): run this BEFORE stopping the launcher. The swap phase in
install.sh separately refuses to proceed while the data-dir lock is held
(destructive steps never race a live launcher); :func:`assert_not_held`
implements the same refusal for programmatic callers.

Platform: Linux-first (P1a). The secrets file and the data-dir lock refuse
Windows loudly on their own (TODO(P3) seams); this module degrades nothing
silently.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess  # nosec B404 — the only exec is a fully argv-pinned pg_dump invocation below (never shell=True)
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from modulo.launcher.env_safety import scrub_os_environment
from modulo.launcher.initdb import _binary
from modulo.launcher.supervisor import _read_lock_holder

_log = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "pre-upgrade-dump-"
SNAPSHOT_MANIFEST_NAME = "manifest.json"
SNAPSHOT_MANIFEST_VERSION = 1
_DUMP_NAME = "database.sql"
_PGDATA_DIRNAME = "pgdata"
_PG_VERSION_FILE = "PG_VERSION"
_POSTGRES_HOST = "127.0.0.1"
_APP_DB_NAME = "modulo"
_SECRETS_FILENAME = "secrets.json"
_STATE_FILENAME = "state.json"
_DUMP_TIMEOUT_SECONDS = 1800
_PRIVATE_MODE = 0o600

# The launcher-owned public surface (consumed by install.sh via the bundled
# runtime; vulture's dead-code gate special-cases __all__).
__all__ = [
    "SNAPSHOT_MANIFEST_NAME",
    "SNAPSHOT_PREFIX",
    "PreUpgradeSnapshot",
    "UpgradeError",
    "assert_not_held",
    "main",
    "pre_upgrade_dump",
]


class UpgradeError(RuntimeError):
    """Raised when the enforced pre-upgrade dump cannot be produced safely."""


@dataclass(frozen=True)
class PreUpgradeSnapshot:
    """One verified pre-upgrade snapshot (the installer aborts without one)."""

    directory: Path
    dump_path: Path
    bytes: int
    created_at: str


def _pid_alive(pid: int) -> bool:
    # /proc exists only on Linux (P1a); elsewhere the holder reads as gone,
    # and the Windows branch of the caller refuses before reaching here.
    return Path(f"/proc/{pid}").exists()


def assert_not_held(data_dir: Path) -> None:
    """Refuse destructive upgrade steps while the data dir is locked.

    Reads the ``<datadir>.lock`` sibling holder record and, on POSIX, checks
    the holder's /proc entry: a LIVE holder refuses (named by PID + mode);
    a dead holder's record is stale — the kernel released the flock when the
    process died — and is only logged. Windows cannot verify flock state
    until its P3 seam: refuse loudly instead of proceeding destructively.

    NOTE: the DUMP wants the bundled stack running (pg_dump talks to live
    Postgres), so the launcher legitimately holds the lock while dumping.
    This refusal is for the SWAP phase, once the snapshot is safe on disk.
    """
    holder = _read_lock_holder(data_dir.parent / (data_dir.name + ".lock"))
    if holder is None:
        return
    if sys.platform == "win32":
        # TODO(P3): holder liveness must be verified with the Windows lock
        # primitive before any destructive step can run unguarded.
        raise UpgradeError(
            f"The data-dir lock record exists (holder PID {holder.pid}, mode {holder.mode!r}) and "
            "holder liveness cannot be verified on Windows yet (TODO(P3)). Stop the launcher, "
            "clear the stale lock record only if the holder process is confirmed dead, then retry."
        )
    if _pid_alive(holder.pid):
        raise UpgradeError(
            f"Refusing: data dir {data_dir} is locked by another launcher (holder PID {holder.pid}, "
            f"mode {holder.mode!r}). Stop that launcher first ('modulo stop'), then re-run — "
            "the snapshot survives; pass --skip-backup on the re-run."
        )
    _log.info("upgrade.stale_lock_record_ignored holder_pid=%s (process is gone)", holder.pid)


def pre_upgrade_dump(
    data_dir: Path,
    *,
    bin_dir: Path | None = None,
    output_dir: Path | None = None,
) -> PreUpgradeSnapshot:
    """Perform and verify the enforced pre-upgrade pg_dump.

    Produces ``<data-dir>/pre-upgrade-dump-<timestamp>/`` containing the
    pg_dump SQL file plus a versioned manifest JSON. The dump MUST be
    non-empty: an empty dump is a failure (the snapshot dir is swept) — the
    installer aborts rather than swapping binaries over a zero-byte snapshot.
    """
    import shutil

    from modulo.launcher.secrets_file import SecretsFileError, load_or_create
    from modulo.launcher.state import load_state

    # FIRST ACTION — the dump must never inherit a launcher-hostile PG*
    # variable (a stale PGPASSWORD/PGHOST would silently redirect it to a
    # foreign endpoint: same contract as the native boot, ADR 031 Decision 2).
    scrub_os_environment()

    state_path = data_dir / _STATE_FILENAME
    if not state_path.exists():
        raise UpgradeError(f"No state.json at {state_path} — the data dir is not bootstrapped; nothing to upgrade")
    pgdata = data_dir / _PGDATA_DIRNAME
    if not (pgdata / _PG_VERSION_FILE).exists():
        raise UpgradeError(f"No initialised bundled cluster at {pgdata} — nothing to dump; upgrade aborted")

    try:
        secrets = load_or_create(data_dir / _SECRETS_FILENAME)
    except SecretsFileError as exc:
        raise UpgradeError(f"secrets unavailable for the pre-upgrade dump: {exc}") from exc
    try:
        state = load_state(state_path, secrets.state_hmac_key)
    except Exception as exc:
        raise UpgradeError(f"state.json at {state_path} cannot be verified: {exc}") from exc

    dump_url = f"postgresql://modulo:{secrets.postgres_password}@{_POSTGRES_HOST}:{state.postgres_port}/{_APP_DB_NAME}"

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    parent = output_dir if output_dir is not None else data_dir
    parent.mkdir(parents=True, exist_ok=True)
    snapshot_dir = parent / f"{SNAPSHOT_PREFIX}{timestamp}"
    counter = 0
    while snapshot_dir.exists():
        counter += 1
        snapshot_dir = parent / f"{SNAPSHOT_PREFIX}{timestamp}-{counter}"
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    dump_path = snapshot_dir / _DUMP_NAME
    argv = [
        _binary(_resolve_pg_bin_dir(bin_dir), "pg_dump"),
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--format=plain",
        dump_url,
    ]
    try:
        _run_dump(argv, dump_path)
    except UpgradeError as exc:
        # A failed dump never leaves a half-written snapshot masquerading
        # as one, and the installer never sees a directory to trust.
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        raise UpgradeError(f"pre-upgrade dump FAILED — upgrade ABORTED, no binary swap was made: {exc}") from exc

    size = dump_path.stat().st_size
    if size <= 0:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        raise UpgradeError(
            "pre-upgrade dump produced an EMPTY file — upgrade ABORTED; refusing to swap binaries over no snapshot"
        )

    created_at = datetime.now(UTC).isoformat()
    manifest = {
        "manifest_version": SNAPSHOT_MANIFEST_VERSION,
        "snapshot_for_upgrade": True,
        "created_at": created_at,
        "dump_name": _DUMP_NAME,
        "dump_bytes": size,
        "postgres_port": state.postgres_port,
    }
    (snapshot_dir / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    if os.name == "posix":
        # TODO(P3): Windows ACL hardening (chmod is a silent no-op there).
        for path in (snapshot_dir, dump_path):
            path.chmod(_PRIVATE_MODE)
    snapshot = PreUpgradeSnapshot(
        directory=snapshot_dir,
        dump_path=dump_path,
        bytes=size,
        created_at=created_at,
    )
    _log.info("upgrade.snapshot_created dir=%s bytes=%s", snapshot_dir, size)
    return snapshot


def _resolve_pg_bin_dir(bin_dir: Path | None) -> Path:
    """Resolve the bundled pg/ binaries directory (param > env > install default)."""
    if bin_dir is not None:
        return bin_dir
    from_env = os.environ.get("MODULO_BUNDLED_BIN_DIR")
    if from_env:
        return Path(from_env)
    return Path(sys.prefix) / "bundled" / "bin"


def _run_dump(argv: list[str], dump_path: Path) -> None:
    """Run one fully-pinned argv pg_dump with output captured to *dump_path*.

    Never shell=True, never operator-controlled input: the dump URL is
    composed from the launcher-owned secrets file the same first boot
    generated. A non-zero exit / timeout becomes :class:`UpgradeError`.
    TODO(P3): Windows seam (the bundled runtime is Linux-first).
    """
    try:
        with dump_path.open("wb") as out:
            completed = subprocess.run(  # nosec B603 —— pinned argv, trusted synthesized input  # noqa: S603
                argv,
                stdout=out,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_DUMP_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(f"pg_dump timed out after {_DUMP_TIMEOUT_SECONDS}s") from exc
    if completed.returncode != 0:
        stderr_text = completed.stderr.decode(errors="replace").strip()
        raise UpgradeError(stderr_text or f"pg_dump exited with code {completed.returncode}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry the installer invokes: dump, verify, print the snapshot path.

    The snapshot directory path is the FINAL stdout line; the installer
    captures it and aborts (non-zero exit) when the helper fails.
    """
    parser = argparse.ArgumentParser(prog="python -m modulo.launcher.upgrade", description="Pre-upgrade pg_dump")
    parser.add_argument("--data-dir", type=Path, required=True, help="Bundled launcher data dir")
    parser.add_argument("--bin-dir", type=Path, default=None, help="Bundled pg/ binaries dir override (packaging seam)")
    args = parser.parse_args(argv)
    scrub_os_environment()
    try:
        snapshot = pre_upgrade_dump(args.data_dir, bin_dir=args.bin_dir)
    except UpgradeError as exc:
        # Actionable installer-abort text: never a bare traceback.
        print(f"ERROR: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    # The installer captures the FINAL stdout line as the snapshot path.
    print(snapshot.directory)  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
