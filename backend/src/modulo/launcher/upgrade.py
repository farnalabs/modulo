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
import hashlib
import json
import logging
import os
import subprocess  # nosec B404 — the only exec is a fully argv-pinned pg_dump invocation below (never shell=True)
import sys
import urllib.parse
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
# The restore-side manifest: `modulo restore` hard-requires backup-info.json
# (shape-validated, checksum-verified). The snapshot carries BOTH manifest
# files so the installer's printed command works verbatim.
_RESTORE_MANIFEST_NAME = "backup-info.json"
_DUMP_NAME = "database.sql"
_PGDATA_DIRNAME = "pgdata"
_PG_VERSION_FILE = "PG_VERSION"
_POSTGRES_HOST = "127.0.0.1"
_APP_DB_NAME = "modulo"
_SECRETS_FILENAME = "secrets.json"
_STATE_FILENAME = "state.json"
_DUMP_TIMEOUT_SECONDS = 1800
_PRIVATE_MODE = 0o600
_DIR_PRIVATE_MODE = 0o700

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


def _decredential_url(raw_url: str) -> tuple[str, str | None]:
    """Split the password out of the dump URL (never bare in /proc cmdline)."""
    parsed = urllib.parse.urlsplit(raw_url)
    password = parsed.password
    if password is None:
        return raw_url, None
    userinfo = ""
    if parsed.username:
        userinfo = urllib.parse.quote(parsed.username, safe="") + "@"
    netloc = f"{userinfo}{parsed.hostname or ''}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=netloc)), urllib.parse.unquote(password)


def _pg_child_env(password: str | None) -> dict[str, str]:
    """Child-scoped env: the password ONLY as PGPASSWORD (never in argv)."""
    env = dict(os.environ)
    env.pop("PGPASSWORD", None)
    if password is not None:
        env["PGPASSWORD"] = password
    return env


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

    from modulo.launcher.env_safety import scrub_os_environment
    from modulo.launcher.secrets_file import SecretsFileError, _load_existing
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

    secrets_path = data_dir / _SECRETS_FILENAME
    if not secrets_path.exists():
        # Load-ONLY: a dump helper that could GENERATE credentials would, on
        # a missing file, silently mint a fresh secrets.json and then fail on
        # state.json HMAC verification — leaving an orphan secrets file that
        # bricks the next boot. Refuse with the remedial message instead.
        raise UpgradeError(
            f"Refusing to dump: the secrets file {secrets_path} is MISSING — the pre-upgrade dump "
            "must never generate credentials. If this data dir was restored from an archive, use the "
            "archive's include-secrets bundle; otherwise the data dir is not bootstrapped. "
            "Do NOT create a secrets.json by hand."
        )
    try:
        secrets = _load_existing(secrets_path)
    except SecretsFileError as exc:
        raise UpgradeError(
            f"secrets file cannot be READ for the pre-upgrade dump (never generated here): {exc}"
        ) from exc
    try:
        state = load_state(state_path, secrets.state_hmac_key)
    except Exception as exc:
        raise UpgradeError(f"state.json at {state_path} cannot be verified: {exc}") from exc

    url_with_password = (
        f"postgresql://modulo:{secrets.postgres_password}@{_POSTGRES_HOST}:{state.postgres_port}/{_APP_DB_NAME}"
    )
    dump_url, postgres_password = _decredential_url(url_with_password)

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
        _run_dump(argv, dump_path, child_env=_pg_child_env(postgres_password))
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
    _write_restore_manifest(snapshot_dir, created_at=created_at)
    if os.name == "posix":
        # TODO(P3): Windows ACL hardening (chmod is a silent no-op there).
        # DIRECTORIES get 0700 — the e(xecute) bit is the DIRECTORY search
        # bit: at 0600 the snapshot dir is unsearchable and even the dump
        # (written moments earlier) can no longer be RESOLVED to read it.
        try:
            snapshot_dir.chmod(_DIR_PRIVATE_MODE)
            for entry in (dump_path, snapshot_dir / SNAPSHOT_MANIFEST_NAME, snapshot_dir / _RESTORE_MANIFEST_NAME):
                entry.chmod(_PRIVATE_MODE)
        except OSError as exc:
            _log.warning("upgrade.snapshot_mode_failed path=%s error=%s", snapshot_dir, exc)
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


def _alembic_heads() -> list[str]:
    """The OLD runtime's current alembic head(s) (unknown → placeholder)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = Path(__file__).resolve().parents[3]  # backend/
        alembic_ini = backend_dir / "alembic.ini"
        if not alembic_ini.exists():
            return ["unknown"]
        script = ScriptDirectory.from_config(Config(str(alembic_ini)))
        return sorted(script.get_heads())
    except Exception as exc:
        _log.warning("upgrade.alembic_heads_unresolved error=%s", exc)
        return ["unknown"]


def _write_restore_manifest(snapshot_dir: Path, *, created_at: str) -> None:
    """Write the restore-compatible manifest the installer's printed command needs.

    ``modulo restore`` hard-requires ``backup-info.json`` (shape-validated +
    checksum-verified). Without it the installer's "restore later with" hint
    refuses immediately. The snapshot is written as a legacy-shaped archive
    (no ``manifest_version`` -> accepted) carrying the dump checksum and the
    schema version at backup time (so restore's downgrade guard can warn or
    refuse). ``fernet_key_hash`` is intentionally absent: re-encryption only
    engages when ``credentials_references.json`` exists, which the snapshot
    does not carry (the full pre-restore DB lives in the SQL dump).
    """
    checksum = hashlib.sha256((snapshot_dir / _DUMP_NAME).read_bytes()).hexdigest()
    restore_manifest = {
        "snapshot_for_upgrade": True,
        "manifest_version": SNAPSHOT_MANIFEST_VERSION,
        "backup_type": "pre-upgrade-snapshot",
        "timestamp": created_at,
        "schema_versions": _alembic_heads(),
        "db_version": "unknown",
        "dump_name": _DUMP_NAME,
        "dump_bytes": (snapshot_dir / _DUMP_NAME).stat().st_size,
        "file_checksums": {_DUMP_NAME: checksum},
    }
    # 0600 fd creation: the manifest is never briefly exposed at umask mode.
    fd = os.open(str(snapshot_dir / _RESTORE_MANIFEST_NAME), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(restore_manifest, f, indent=2, sort_keys=True)


def _run_dump(argv: list[str], dump_path: Path, *, child_env: dict[str, str] | None = None) -> None:
    """Run one fully-pinned argv pg_dump with output captured to *dump_path*.

    Never shell=True, never operator-controlled input: the dump URL is
    composed from the launcher-owned secrets file the same first boot
    generated, with the password OUT OF ARGV (world-readable /proc cmdline)
    and inside the child-scoped env dict instead. The dump file is CREATED
    0600 so a mid-dump SIGKILL never leaves it readable at umask mode. A
    non-zero exit / timeout becomes :class:`UpgradeError`.
    TODO(P3): Windows seam (the bundled runtime is Linux-first).
    """
    fd = os.open(str(dump_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    try:
        with os.fdopen(fd, "wb") as out:
            completed = subprocess.run(  # nosec B603 —— pinned argv, trusted synthesized input  # noqa: S603
                argv,
                stdout=out,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_DUMP_TIMEOUT_SECONDS,
                env=child_env if child_env is not None else dict(os.environ),
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
