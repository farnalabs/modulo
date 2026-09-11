"""Installer-enforced pre-upgrade pg_dump orchestration (FAR-672 / ADR 031 Decision 7).

P1a upgrade flow = rerun the installer. The installer ENFORCES a pre-upgrade
pg_dump before it touches anything version-specific: it calls the OLD
installation's bundled runtime here (``python -m modulo.launcher.upgrade``),
which resolves the BUNDLED pg_dump client, dumps the bundled app database to
a versioned snapshot directory inside the data dir, verifies the dump is
non-empty, and prints the snapshot path as its final stdout line.

On ANY dump failure the helper raises :class:`UpgradeError` and the installer
ABORTS â€” no binary swap happens without a verified snapshot. ``--skip-backup``
in install.sh is the operator's loud, explicit escape hatch for the SECOND
installer run (the dump already succeeded; the stack must stay stopped for
the swap â€” see the install.sh comment block).

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
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess  # nosec B404 â€” the only exec is a fully argv-pinned pg_dump invocation below (never shell=True)
import sys
import tarfile
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modulo.launcher import manifest as manifest_module
from modulo.launcher.env_safety import scrub_os_environment
from modulo.launcher.initdb import _binary
from modulo.launcher.manifest import ManifestSecurityError, ReleaseManifest
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
    "UpgradeResult",
    "assert_not_held",
    "atomic_symlink_swap",
    "check_no_downgrade",
    "default_install_root",
    "extract_and_verify_bundle",
    "fetch_release_assets",
    "main",
    "perform_upgrade",
    "pre_upgrade_dump",
    "prune_versions",
    "repair_current_symlink",
    "restore_guidance",
    "sweep_upgrade_caches",
    "write_upgrade_marker",
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
    a dead holder's record is stale â€” the kernel released the flock when the
    process died â€” and is only logged. Windows cannot verify flock state
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
            f"mode {holder.mode!r}). Stop that launcher first ('modulo stop'), then re-run â€” "
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
    non-empty: an empty dump is a failure (the snapshot dir is swept) â€” the
    installer aborts rather than swapping binaries over a zero-byte snapshot.
    """
    import shutil

    from modulo.launcher.env_safety import scrub_os_environment
    from modulo.launcher.secrets_file import SecretsFileError, _load_existing
    from modulo.launcher.state import load_state

    # FIRST ACTION â€” the dump must never inherit a launcher-hostile PG*
    # variable (a stale PGPASSWORD/PGHOST would silently redirect it to a
    # foreign endpoint: same contract as the native boot, ADR 031 Decision 2).
    scrub_os_environment()

    state_path = data_dir / _STATE_FILENAME
    if not state_path.exists():
        raise UpgradeError(f"No state.json at {state_path} â€” the data dir is not bootstrapped; nothing to upgrade")
    pgdata = data_dir / _PGDATA_DIRNAME
    if not (pgdata / _PG_VERSION_FILE).exists():
        raise UpgradeError(f"No initialised bundled cluster at {pgdata} â€” nothing to dump; upgrade aborted")

    secrets_path = data_dir / _SECRETS_FILENAME
    if not secrets_path.exists():
        # Load-ONLY: a dump helper that could GENERATE credentials would, on
        # a missing file, silently mint a fresh secrets.json and then fail on
        # state.json HMAC verification â€” leaving an orphan secrets file that
        # bricks the next boot. Refuse with the remedial message instead.
        raise UpgradeError(
            f"Refusing to dump: the secrets file {secrets_path} is MISSING â€” the pre-upgrade dump "
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
        raise UpgradeError(f"pre-upgrade dump FAILED â€” upgrade ABORTED, no binary swap was made: {exc}") from exc

    size = dump_path.stat().st_size
    if size <= 0:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        raise UpgradeError(
            "pre-upgrade dump produced an EMPTY file â€” upgrade ABORTED; refusing to swap binaries over no snapshot"
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
        # DIRECTORIES get 0700 â€” the e(xecute) bit is the DIRECTORY search
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
    """The OLD runtime's current alembic head(s) (unknown â†- placeholder)."""
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
            completed = subprocess.run(  # nosec B603 â€”â€” pinned argv, trusted synthesized input  # noqa: S603
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


# ---------------------------------------------------------------------------
# The full `modulo upgrade` flow (FAR-675, ADR 031 Decision 7)
#
# Stage order (destructive last):
#   1. fetch the target bundle; SIGNATURE-verify the release manifest;
#      verify the tarball sha256 against it BEFORE extraction. --from-file
#      is the offline escape hatch (operator-provided manifest + sig).
#   2. Pre-flight refusals BEFORE the swap: PG major vs PG_VERSION (hard,
#      never auto-initdb; the manual pg_upgrade remediation per the ADR's
#      bundled-PG-16-only policy); downgrade (DB revision ahead of the
#      target head); quantified disk incl. dump headroom.
#   3. ENFORCED pre-upgrade pg_dump (--skip-backup is the loud flag).
#   4. Service-aware stop (the FAR-674 systemd user unit when installed,
#      else the foreground lock holder) + no-live-process abort (exe/cwd
#      scan naming the PID - the locked-file failure class).
#   5. Atomic `current` symlink swap (POSIX rename(2); Windows
#      junction/rename swap = TODO(P3)) + the upgrade.json marker written
#      BEFORE the boot (the FAR-674 degraded-within-N seam).
#   6. Boot the new bundle's lifespan migrations via its own launcher
#      (`launcher start`) + /healthz gate; on failure: hard refusal
#      printing the snapshot path + the exact manual restore command
#      restoring BOTH binary AND data (forward-migrated-schema wording).
#      NO automatic restore in v1 - the fast-follow design is recorded in
#      ADR 031 Decision 7 (referenced in the message).
#   7. Retention: keep the last 2 version dirs; sweep caches; NEVER delete
#      a state.json-referenced dir; repair a dangling `current` symlink.
# ---------------------------------------------------------------------------

INSTALL_ROOT_ENV = "MODULO_INSTALL_ROOT"
UPGRADE_MARKER_FILENAME = "upgrade.json"
UPGRADE_MARKER_SCHEMA_VERSION = 1
_RETAINED_VERSIONS = 2
_CACHE_PREFIXES = (".staging-", ".downloads-", ".current.new.")
_DEFAULT_BOOT_TIMEOUT = 600.0
_BOOT_POLL_INTERVAL = 0.5
_RELEASES_DOWNLOAD_BASE = "https://github.com/farnalabs/modulo/releases/download"
_RELEASE_MANIFEST_NAME = "RELEASE_MANIFEST.json"
_LAUNCHER_NAME = "launcher"
_PRESSURE_RESTORE_MANIFEST_NAME = _RESTORE_MANIFEST_NAME


def default_install_root() -> Path:
    """Per-user install root (install.sh's own default + env override)."""
    from_env = os.environ.get(INSTALL_ROOT_ENV)
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / ".local" / "opt" / "modulo"


def _strip_bundle_prefix(version_text: str) -> str:
    """The version-dir name for `bundle-v<v>` / `v<v>` / bare `<v>` targets."""
    return version_text.removeprefix("bundle-v").removeprefix("v")


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """0600-creation JSON write, fsynced before close.

    The fsync closes the crash window: an upgrade.json marker written just
    before a power loss must be on disk when the next boot reads it (the
    FAR-674 degraded-window seam keys off exactly this file).
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def _sha256_file(path: Path) -> str:
    """Chunked sha256 (release artifacts exceed memory)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_sort_key(name: str) -> tuple[int, int, int]:
    """Numeric version ordering (1.2.10 sorts after 1.2.9)."""
    numbers: list[int] = []
    for chunk in name.split("."):
        try:
            numbers.append(int(chunk))
        except ValueError:
            numbers.append(-1)
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


@dataclass(frozen=True)
class UpgradeResult:
    """Outcome of one completed `modulo upgrade` run."""

    previous_version: str
    version: str
    snapshot: Path | None
    marker_path: Path
    pruned: list[str]


def write_upgrade_marker(
    data_dir: Path,
    *,
    previous_version: str,
    version: str,
    pre_upgrade_snapshot: Path | None,
    upgraded_at: float,
) -> Path:
    """Write upgrade.json BEFORE the new bundle boots (FAR-674 seam writer).

    The entry's degraded-context provider opens this marker when a degrade
    lands within ``policy.UPGRADE_DEGRADED_WINDOW_SECONDS`` of
    ``upgraded_at`` - recorded at the marker write, before any boot.
    """
    marker_path = data_dir / UPGRADE_MARKER_FILENAME
    payload = {
        "schema_version": UPGRADE_MARKER_SCHEMA_VERSION,
        "upgraded_at": upgraded_at,
        "previous_version": previous_version,
        "target_version": version,
        "pre_upgrade_snapshot": str(pre_upgrade_snapshot) if pre_upgrade_snapshot is not None else None,
    }
    _write_private_json(marker_path, payload)
    return marker_path


def restore_guidance(
    *,
    snapshot: Path | None,
    previous_version: str,
    install_root: Path,
    data_dir: Path,
    prior_link_target: str | None = None,
) -> str:
    """The exact manual restore command restoring BOTH binary AND data.

    Explicit forward-migrated-schema wording: the old binary is NEVER
    restored alone - the attempted upgrade's boot may have
    forward-migrated the schema past the old bundle's head. NO automatic
    restore ran (none exists in v1; the fast-follow design - temp dir +
    verify + atomic exchange, gated on never-reached-healthy - is recorded
    in ADR 031 Decision 7, referenced here per the ADR policy).

    *prior_link_target* names the version dir that still holds the OLD
    binary's bytes: a re-run of the same version keeps the previous bytes
    at ``versions/<v>.prev-<ts>``, so the link target names THAT dir and
    never the fresh copy sitting at ``versions/<v>``.
    """
    head = (
        "Manual restore - this restores BOTH the binary AND the data. The attempted upgrade's\n"
        "boot may have forward-migrated the database schema past the old bundle's migration\n"
        "head, so restoring the old binary ALONE is not sufficient."
    )
    fault = "NO automatic restore ran (none exists in v1; the fast-follow design is recorded in\nADR 031 Decision 7)."
    link_version = prior_link_target if prior_link_target is not None else previous_version
    if snapshot is None:
        link_cmd = f"ln -sfn versions/{link_version} {install_root / 'current'}"
        return f"{fault}\n{head}\nExact manual restore command:\n  {link_cmd}"
    restore_cmd = f"modulo restore {snapshot} --data-dir {data_dir} --yes"
    link_cmd = f"ln -sfn versions/{link_version} {install_root / 'current'}"
    compound = link_cmd + " && " + restore_cmd
    return f"{fault}\n{head}\nPre-upgrade snapshot: {snapshot}\nExact manual restore command:\n  {compound}"


def _temp_owner_pid(name: str) -> int | None:
    """The trailing pid suffix of a swap/sweep temp name, or None."""
    match = re.search(r"(\d+)$", name)
    return int(match.group(1)) if match is not None else None


def sweep_symlink_temps(link_path: Path) -> None:
    """Remove stale `.current.new.<pid>` temps from earlier crashed swaps.

    A temp owned by a LIVE pid is another concurrent swap's in-flight link —
    never swept (only the owning process's crash leaves it stale).
    """
    with contextlib.suppress(OSError):
        for stale in link_path.parent.glob(f".{link_path.name}.new.*"):
            owner_pid = _temp_owner_pid(stale.name)
            if owner_pid is not None and _pid_alive(owner_pid):
                continue
            with contextlib.suppress(OSError):
                if stale.is_symlink() or stale.is_file():
                    stale.unlink()
                elif stale.is_dir():  # pragma: no cover - unexpected
                    shutil.rmtree(stale, ignore_errors=True)


def sweep_upgrade_caches(install_root: Path) -> list[str]:
    """Sweep stale `.staging-*` / `.downloads-*` / `.current.new.*` debris.

    The download/extract caches land here when an interrupted run dies;
    retention NEVER touches a state.json-referenced (versioned) dir. Temps
    carrying a LIVE pid suffix are concurrent runs' working state — skipped.
    """
    pruned: list[str] = []
    if not install_root.is_dir():
        return pruned
    with contextlib.suppress(OSError):
        for entry in install_root.iterdir():
            if not any(entry.name.startswith(prefix) for prefix in _CACHE_PREFIXES):
                continue
            owner_pid = _temp_owner_pid(entry.name)
            if owner_pid is not None and _pid_alive(owner_pid):
                continue
            try:
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
            except OSError:
                continue
            pruned.append(entry.name)
    return pruned


def atomic_symlink_swap(link_path: Path, target_value: str) -> None:
    """The POSIX crash-safe `current` rename swap (the swap primitive).

    A temp sibling symlink is built then rename(2)'d OVER the incumbent, so
    a crash mid-swap leaves either the old or the new link - never a
    missing `current`. Stale temps from earlier crashed swaps are swept
    first; a hard failure (a locked/dead target, a full volume) RAISES
    rather than half-swapping. TODO(P3): the Windows junction/rename swap
    seam substitutes for the POSIX symlink swap here.
    """
    if sys.platform == "win32":
        raise UpgradeError(
            "the `current` symlink swap is POSIX-only (TODO(P3): the Windows seam swaps over a junction via rename)"
        )
    sweep_symlink_temps(link_path)
    temp_link = link_path.parent / f".{link_path.name}.new.{os.getpid()}"
    with contextlib.suppress(OSError):
        temp_link.unlink()
    temp_link.symlink_to(target_value)
    try:
        temp_link.replace(link_path)
    except OSError:
        with contextlib.suppress(OSError):
            temp_link.unlink()
        raise


def no_live_process_inside(target_root: Path) -> None:
    """Refuse when a LIVE process has exe or cwd inside *target_root*.

    Covers the locked-file failure class: a still-running binary/library
    from the version dir about to be swapped. Names every offending PID.
    The UPGRADING PROCESS ITSELF is excluded: self-upgrades re-run the CLI
    whose executable shim lives inside the scanned incumbent dir — without
    the self-exclusion every self-upgrade aborts on its own process.
    POSIX-only (the /proc scan); the flow's own platform gate refuses the
    remaining platforms before reaching here.
    """
    if os.name != "posix":
        # The flow's own platform gate refuses non-POSIX first; nothing on
        # that path reads /proc (the Linux-first P1a scan shape).
        return
    resolved_root = os.path.realpath(str(target_root))
    own_pid = os.getpid()
    own_exe = os.path.realpath(sys.executable)
    offenders: list[str] = []
    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue
        pid = int(proc_entry.name)
        try:
            exe = str((proc_entry / "exe").readlink())
            cwd = str((proc_entry / "cwd").readlink())
        except OSError:
            continue
        if pid == own_pid or os.path.realpath(str(exe)) == own_exe:
            continue
        cwd_real = os.path.realpath(str(cwd))
        if (
            exe == resolved_root
            or exe.startswith(resolved_root + os.sep)
            or cwd_real == resolved_root
            or cwd_real.startswith(resolved_root + os.sep)
        ):
            offenders.append(f"pid {proc_entry.name} (cwd={cwd}, exe={exe})")
    if offenders:
        raise UpgradeError(
            f"Refusing: live processes remain inside {resolved_root} - stop them first:\n  " + "\n  ".join(offenders)
        )


def repair_current_symlink(install_root: Path) -> str | None:
    """Repair a dangling `current` symlink to last-known-good.

    Re-points (atomic_symlink_swap) to the newest remaining version dir.
    NEVER deletes anything - the resolved `current` target is the version
    bookkeeping treats as the data dir's live companion.
    """
    link = install_root / "current"
    versioned_root = install_root / "versions"
    if not versioned_root.is_dir():
        return None
    names = sorted(
        (entry.name for entry in versioned_root.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
        key=_version_sort_key,
        reverse=True,
    )
    if not names:
        return None
    if link.is_symlink():
        try:
            resolved_link = link.resolve(strict=True)
        except OSError:
            resolved_link = None
        if resolved_link is not None and resolved_link.is_dir():
            return None  # not dangling: nothing to repair
    sweep_symlink_temps(link)
    temp_link = link.parent / f".{link.name}.repair.{os.getpid()}"
    temp_link.symlink_to(f"versions/{names[0]}")
    try:
        temp_link.replace(link)
    except OSError:
        with contextlib.suppress(OSError):
            temp_link.unlink()
        raise
    return names[0]


def prune_versions(
    install_root: Path,
    *,
    current_target: Path,
    retained: int = _RETAINED_VERSIONS,
) -> list[str]:
    """Keep the newest *retained* version dirs; never delete the live one.

    `current` is RE-RESOLVED at prune time (the swap may have re-pointed it
    moments ago — pruning against the pre-swap incumbent can delete the dir
    `current` now serves) and the state.json-referenced dir
    (*current_target*) — neither is ever deleted. `.prev-<ts>` re-run
    retention dirs are pruned only at their own later cycle, never here.
    """
    versioned_root = install_root / "versions"
    protected: set[Path] = {current_target.resolve()}
    link = install_root / "current"
    if link.is_symlink():
        with contextlib.suppress(OSError):
            resolved_now = link.resolve(strict=True)
            if resolved_now.is_dir():
                protected.add(resolved_now.resolve())
    entries = sorted(
        (
            entry
            for entry in versioned_root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".") and ".prev-" not in entry.name
        ),
        key=lambda entry: _version_sort_key(entry.name),
        reverse=True,
    )
    pruned: list[str] = []
    walked = 0
    for entry in entries:
        if entry.resolve() in protected:
            continue
        walked += 1
        if walked <= retained:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        pruned.append(entry.name)
    return pruned


def _host_arch() -> str:
    """The bundle asset's arch token (modulo-<v>-linux-<arch>.tar.gz)."""
    import platform

    return {
        "x86_64": "amd64",
        "AMD64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine(), "amd64")


def _urllib_fetch(url: str, target: Path) -> None:
    """argv-free pinned-https fetch (urllib only - no shell, no curl argv)."""
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as handle:  # noqa: S310  # nosec B310 - pinned https release base
        shutil.copyfileobj(response, handle)


@dataclass(frozen=True)
class UpgradeDownloads:
    """The fetched, not-yet-verified release assets."""

    version: str
    tarball: Path
    manifest: Path
    signature: Path


def fetch_release_assets(
    target_version: str,
    download_dir: Path,
    *,
    fetch: Callable[[str, Path], None] | None = None,
) -> UpgradeDownloads:
    """Fetch bundle-v<v>'s tarball + signed manifest + .sig (pinned https).

    A pinned version is REQUIRED (the /releases/latest resolution is a
    TODO seam); the argv-free urllib fetch carries no shell and no curl
    argv. The signed manifest lands beside the tarball so the sha256 gate
    runs BEFORE extraction.
    """
    release_tag = f"bundle-v{target_version}"
    download_dir.mkdir(parents=True, exist_ok=True)
    tarball_name = f"modulo-{target_version}-linux-{_host_arch()}.tar.gz"
    base = f"{_RELEASES_DOWNLOAD_BASE}/{release_tag}"
    tarball = download_dir / tarball_name
    manifest_file = download_dir / _RELEASE_MANIFEST_NAME
    signature_file = download_dir / f"{_RELEASE_MANIFEST_NAME}.sig"
    fetch_fn = fetch if fetch is not None else _urllib_fetch
    fetch_fn(f"{base}/{tarball_name}", tarball)
    fetch_fn(f"{base}/{_RELEASE_MANIFEST_NAME}", manifest_file)
    fetch_fn(f"{base}/{_RELEASE_MANIFEST_NAME}.sig", signature_file)
    return UpgradeDownloads(version=target_version, tarball=tarball, manifest=manifest_file, signature=signature_file)


def extract_and_verify_bundle(tarball: Path, staging_dir: Path) -> Path:
    """Extract into *staging_dir*; assert ONE top-level dir; verify each
    file against the bundle's own SHA256SUMS (the second integrity layer)."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            archive.extractall(staging_dir, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise UpgradeError(f"could not extract {tarball.name}: {exc}") from exc
    top_levels = [entry for entry in staging_dir.iterdir() if entry.is_dir()]
    if len(top_levels) != 1:
        raise UpgradeError(
            f"unexpected archive layout: expected exactly one top-level directory in "
            f"{tarball.name}, found {len(top_levels)}"
        )
    bundle_dir = top_levels[0]
    sums_path = bundle_dir / "SHA256SUMS"
    if not sums_path.is_file():
        raise UpgradeError(f"malformed bundle: SHA256SUMS missing from {tarball.name}")
    for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        expected, _separator, name = line.partition("  ")
        if len(expected) != 64 or not name:
            raise UpgradeError(f"malformed SHA256SUMS line in {tarball.name}: {line!r}")
        artifact = bundle_dir / name
        if not artifact.is_file():
            raise UpgradeError(f"bundle integrity check FAILED: {name} (listed in SHA256SUMS) is missing")
        actual = _sha256_file(artifact)
        if actual != expected:
            raise UpgradeError(
                f"bundle integrity check FAILED for {name}: expected {expected}, got {actual} - "
                "do not use this download; report it at https://github.com/farnalabs/modulo/issues"
            )
    _log.info("upgrade.bundle_internal_checksums_verified dir=%s", bundle_dir.name)
    return bundle_dir


def _prior_pg_bin(current_target: Path) -> Path:
    """The OLD bundle's bundled pg/ binaries dir (for the enforced dump)."""
    pg_bin = current_target / "pg" / "bin"
    if not pg_bin.is_dir():
        raise UpgradeError(f"the installed bundle carries no pg/ binaries at {pg_bin}")
    return pg_bin


def stop_running_stack(data_dir: Path, *, unit_file: Path | None = None) -> None:
    """Service-aware stop: the FAR-674 systemd user unit when installed
    (stopped FIRST - a restart=on-failure unit would otherwise respawn the
    foreground holder), then the SIGTERM via the supervisor's stop;
    refuses with the holder PID when the stack is STILL live afterwards.

    A systemctl-stop timeout is handled (logged loudly, never a traceback):
    the supervisor's own request_stop still runs, and any residual holder
    produces the refusal path below.
    """
    from modulo.launcher import service as service_module
    from modulo.launcher.supervisor import request_stop

    unit_path = unit_file if unit_file is not None else service_module.default_unit_path()
    if unit_path.is_file():
        try:
            stop_result = subprocess.run(  # noqa: S603
                ["systemctl", "--user", "stop", service_module.UNIT_FILENAME],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if stop_result.returncode != 0:
                _log.warning(
                    "upgrade.unit_stop_failed rc=%s stderr=%s",
                    stop_result.returncode,
                    stop_result.stderr[-300:],
                )
        except subprocess.TimeoutExpired:
            _log.warning("upgrade.unit_stop_timed_out after 30s - falling through to the supervisor stop")
    request_stop(data_dir)
    holder = _read_lock_holder(data_dir.parent / (data_dir.name + ".lock"))
    if holder is not None and _pid_alive(holder.pid):
        _restart_unit_best_effort(data_dir, why="post-stop abort: restoring the stopped service")
        raise UpgradeError(
            f"Refusing: the data dir {data_dir} is STILL locked by a live launcher (holder PID "
            f"{holder.pid}) - stop it first: modulo stop"
        )


def _restart_unit_best_effort(data_dir: Path, *, why: str) -> None:
    """Best-effort `systemctl --user start` of the FAR-674 unit (+ loud log).

    Post-stop aborts otherwise leave the bundled stack DARK: the unit was
    stopped, the supervisor's stop already ran, and no path restarts it.
    The stack is restarted whenever a unit exists so a refusal never has a
    dark side-effect; without the unit there is nothing to restart (the
    foreground launcher either lives or was never running).
    """
    try:
        from modulo.launcher import service as service_module

        if not service_module.default_unit_path().is_file():
            return
        restart = subprocess.run(  # noqa: S603
            ["systemctl", "--user", "start", service_module.UNIT_FILENAME],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if restart.returncode == 0:
            _log.warning("upgrade.unit_started_after_abort reason=%s", why)
        else:
            _log.error(
                "upgrade.unit_restart_after_abort_FAILED rc=%s stderr=%s (%s)",
                restart.returncode,
                restart.stderr[-300:],
                why,
            )
    except Exception:  # best-effort path, never masks the primary error
        _log.exception("upgrade.unit_restart_best_effort_failed (%s)", why)


def _snapshot_schema_versions(snapshot_dir: Path) -> list[str]:
    """The pre-upgrade snapshot's recorded alembic head(s) (restore side)."""
    recorded = snapshot_dir / _RESTORE_MANIFEST_NAME
    if not recorded.is_file():
        return []
    try:
        body = json.loads(recorded.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"cannot read the snapshot's recorded schema versions: {exc}") from exc
    versions = body.get("schema_versions")
    return [str(entry) for entry in versions] if isinstance(versions, list) else []


def _bundle_alembic_revisions(bundle_dir: Path) -> set[str]:
    """Every revision id the NEW bundle's migrations tree ships."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        migrations_dir = bundle_dir / "backend" / "src" / "modulo" / "db" / "migrations"
        if not migrations_dir.is_dir():
            return set()
        config = Config()
        config.set_main_option("script_location", str(migrations_dir))
        script = ScriptDirectory.from_config(config)
        return {revision.revision for revision in script.walk_revisions()}
    except Exception as exc:
        _log.warning("upgrade.target_heads_unresolved error=%s", exc)
        return set()


def check_no_downgrade(
    current_heads: list[str],
    snapshot_dir: Path | None,
    bundle_dir: Path,
) -> None:
    """Refuse a downgrade: DB revision ahead of the target bundle's head.

    *current_heads* is the LIVE DB side (the caller passes the old runtime's
    live alembic probe on a skip-backup run, the snapshot's recorded heads
    otherwise). "unknown" is never pass-through: when the live side reads
    unknown and a snapshot exists, the snapshot's recorded schema versions
    answer instead - and if BOTH read unknown (or nothing at all), the
    check REFUSES blind, never guesses. The refusal carries the restore
    guidance wording.
    """
    target_heads = _bundle_alembic_revisions(bundle_dir)
    db_heads = list(current_heads)
    if snapshot_dir is not None and (not db_heads or "unknown" in db_heads):
        db_heads = _snapshot_schema_versions(snapshot_dir)
    unknown = [head for head in db_heads if head == "unknown"]
    if not db_heads or len(unknown) == len(db_heads):
        raise UpgradeError(
            "cannot verify the database's migration revision(s): neither the live probe nor "
            "the pre-upgrade snapshot recorded them - refusing the downgrade check blind"
        )
    unknown_to_target = [head for head in db_heads if head not in target_heads and head != "unknown"]
    if unknown_to_target:
        raise UpgradeError(
            "Refusing to DOWNGRADE: the database revision(s) "
            f"{', '.join(unknown_to_target)} are ahead of the target bundle's supported "
            "migration head.\n"
            "Restore guidance: re-point `current` back to the previous version (ln -sfn "
            "versions/<previous> <install-root>/current) and restore the pre-upgrade snapshot "
            "(modulo restore <snapshot-dir> --yes)."
        )


def _check_pg_major(components: dict[str, str], data_dir: Path) -> None:
    """PG major vs the data dir's PG_VERSION - a mismatch HARD-refuses.

    Per ADR 031 Decision 7: bundled PostgreSQL is 16.x-only, never
    auto-initdb; the manual pg_upgrade remediation names the ADR policy
    (the pg-upgrade helper is a merge prerequisite of any future PG-major
    bump; the bump itself waits for PG 16 EOL ~Nov 2028 or a high-severity
    CVE).
    """
    pg_version = str(components.get("postgres", "")).strip()
    if not pg_version or not re.fullmatch(r"\d+(?:\.\d+)*", pg_version):
        raise UpgradeError(
            f"the release manifest's 'components' carry no USABLE postgres version ({pg_version!r}) "
            "- refusing the upgrade (a version the pg-major gate cannot compare is never guessed)"
        )
    target_major = int(pg_version.split(".")[0])
    version_file = data_dir / _PGDATA_DIRNAME / _PG_VERSION_FILE
    if not version_file.exists():
        raise UpgradeError(
            f"no initialised bundled cluster at {data_dir / _PGDATA_DIRNAME} - refusing the "
            "upgrade (never auto-initdb over a data dir)"
        )
    try:
        data_major = int(version_file.read_text(encoding="utf-8").strip().split(".")[0])
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"cannot read {version_file}: {exc}") from exc
    if data_major != target_major:
        raise UpgradeError(
            f"Bundled PostgreSQL major mismatch: the data dir at {data_dir} was initialised by "
            f"PostgreSQL {data_major}, but the target bundle ships PostgreSQL {target_major}.\n"
            "Per ADR 031 Decision 7, bundled PostgreSQL is 16.x-only and a PG-major mismatch is\n"
            "HARD-REFUSED - never auto-initdb. Manual remediation: run pg_upgrade against this\n"
            "data dir BEFORE upgrading (the pg-upgrade helper is a REQUIRED merge prerequisite\n"
            "of the first PG-major-bump release per the same ADR; until it ships there is no\n"
            "supported upgrade across a PG major)."
        )


def _bundled_boot_argv(install_root: Path, version: str, data_dir: Path) -> list[str]:
    """The NEW bundle's boot argv (the documented migration mechanism).

    Mechanism choice: the flow EXECs the NEW bundle's own `launcher start`
    console hook as a FOREGROUND child; the launcher's lifespan migrations
    run as part of that boot (ADR 031 Decision 2 - SAQ spawns only after
    migration-head readiness), so no separate migration exec exists. The
    /healthz gate (run_boot) decides healthy.
    """
    launcher = install_root / "current" / _LAUNCHER_NAME
    if not launcher.is_file():
        raise UpgradeError(
            f"the NEW bundle carries no launcher hook ({launcher}) - refusing to boot "
            "(the FAR-671 bundle layout is in place, but the launcher hook is absent)"
        )
    return [str(launcher), "start", "--data-dir", str(data_dir)]


def run_boot(
    boot_argv: list[str],
    *,
    api_port: int,
    timeout: float = _DEFAULT_BOOT_TIMEOUT,
    poll_interval: float = _BOOT_POLL_INTERVAL,
) -> int:
    """Exec the NEW bundle's launch and gate on /healthz.

    subprocess.Popen the boot argv in its OWN process group
    (``start_new_session=True``): the bundled postgres/redis/SAQ children
    belong to the same group, so teardown never orphans them. stderr is a
    temp FILE (never a PIPE): a chatty child cannot fill a 64KB pipe and
    deadlock while /healthz is never served - a false timeout referral -
    and the failure path reads the file for diagnostics.

    Health gate = an EXACT HTTP 200 whose body is ``{"status": "ok"}``:
    /healthz is the launcher's liveness endpoint (its migration gate - the
    lifespan runs the migrations BEFORE the API serves). The current
    endpoint carries no version/head field; when the launcher ships one
    the upgrade flow should assert it here (TODO adjacent), and the boot's
    own migration readiness is additionally the launcher's OWN gate -
    /healthz freshness is honest about being liveness-only.

    The caller decides the resting state (see perform_upgrade): a healthy
    gate-restores the stack when the FAR-674 systemd unit exists (no
    systemd unit -> the flow leaves the stack stopped and says so).

    A failing boot / no health inside *timeout* -> UpgradeError; the
    teardown is a GROUP kill (SIGTERM to the group, escalated).
    """
    import urllib.error
    import urllib.request

    launcher = Path(boot_argv[0])
    stderr_allowance = 65536
    stderr_file = tempfile.TemporaryFile(prefix="modulo-upgrade-boot-stderr-")  # noqa: SIM115 - the lifetime must span Popen + the poll loop
    process = subprocess.Popen(  # noqa: S603 - pinned argv, synthesized trusted input
        boot_argv,
        cwd=str(launcher.parent),
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
        start_new_session=(sys.platform != "win32"),
    )
    health_url = f"http://127.0.0.1:{api_port}/healthz"
    started_at = time.monotonic()
    healthy = False
    try:
        while process.poll() is None:
            try:
                with urllib.request.urlopen(health_url, timeout=2) as response:  # nosec B310 - loopback-only health probe
                    if response.status == 200:
                        body = json.loads(response.read().decode("utf-8", errors="replace"))
                        if isinstance(body, dict) and body.get("status") == "ok":
                            healthy = True
                            break
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
                pass
            if time.monotonic() - started_at > timeout:
                break
            time.sleep(poll_interval)
    finally:
        _terminate_boot_process_group(process)
        stderr_file.seek(0)
        stderr_text = stderr_file.read(stderr_allowance).decode("utf-8", errors="replace").strip()
        stderr_file.close()
    if not healthy:
        raise UpgradeError(
            f"the NEW bundle's boot did not reach a healthy /healthz (child exit code "
            f"{process.returncode}): {stderr_text[-400:] if stderr_text else '(no stderr)'}"
        )
    return int(process.returncode or 0)


def _terminate_boot_process_group(process: subprocess.Popen[bytes]) -> None:
    """Ordered teardown of the boot child's WHOLE process group.

    The bundled postgres/redis/SAQ children share the group, so the direct
    child is never orphaned owning a live stack. SIGTERM first, a short
    wait, then SIGKILL - on every exit path (timeout AND crash).
    """
    if process.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            process_group = os.getpgid(process.pid)
            os.killpg(process_group, signal.SIGTERM)
        else:  # pragma: no cover - the platform gate refuses Windows earlier
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=30)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
        process.wait(timeout=10)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        _log.exception("upgrade.boot_group_kill_incomplete pid=%s", process.pid)


def _quantified_disk_preflight(tarball: Path, data_dir: Path) -> None:
    """Quantified free-space gate (bundle staging + dump headroom).

    The bound: (tarball bytes) x 2 (staging + extract) + (the pgdata
    footprint) x 2 (the enforced pg_dump plus its headroom).
    """
    pgdata = data_dir / _PGDATA_DIRNAME
    tarball_bytes = tarball.stat().st_size
    pgdata_bytes = 0
    try:
        for entry in pgdata.rglob("*"):
            if entry.is_file():
                pgdata_bytes += entry.stat().st_size
    except OSError as exc:
        raise UpgradeError(f"cannot measure the pgdata footprint for the disk preflight: {exc}") from exc
    required = 2 * tarball_bytes + 2 * pgdata_bytes
    free = shutil.disk_usage(str(data_dir)).free
    if free < required:
        raise UpgradeError(
            f"Insufficient disk: {required} bytes required "
            f"({2 * tarball_bytes} bundle staging + {2 * pgdata_bytes} dump headroom), "
            f"{free} bytes free on the volume holding {data_dir}"
        )


def _move_into_place(bundle_dir: Path, install_root: Path, version: str) -> tuple[Path, Path | None]:
    """Lay the staged bundle down as versions/<version> (the v1 layout).

    A re-run over the SAME version keeps the old bytes at
    ``versions/<v>.prev-<ts>`` (never destroyed before the boot gate):
    restore_guidance then names that surviving dir as the link target.
    """
    versioned_root = install_root / "versions"
    versioned_root.mkdir(parents=True, exist_ok=True)
    target = versioned_root / version
    prior_dir: Path | None = None
    if target.exists():
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        prior_dir = versioned_root / f"{target.name}.prev-{timestamp}"
        counter = 0
        while prior_dir.exists():
            counter += 1
            prior_dir = versioned_root / f"{target.name}.prev-{timestamp}-{counter}"
        with contextlib.suppress(OSError):
            target.rename(prior_dir)
            prior_dir = prior_dir if prior_dir.is_dir() else None
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(bundle_dir, target, symlinks=True)
    shutil.rmtree(bundle_dir, ignore_errors=True)
    _log.info("upgrade.installed_target dir=%s prior=%s", target, prior_dir)
    return target, prior_dir


def _api_port_of(data_dir: Path) -> int:
    """The persisted API port (state.json via the secrets-keyed loader)."""
    from modulo.launcher.secrets_file import _load_existing
    from modulo.launcher.state import load_state

    secrets = _load_existing(data_dir / _SECRETS_FILENAME)
    owned_state = load_state(data_dir / _STATE_FILENAME, secrets.state_hmac_key)
    return int(owned_state.api_port)


def _resolve_current_target(install_root: Path) -> Path:
    """The incumbent (state.json-referenced) version dir - never deleted.

    A DANGLING `current` is repaired first (repair_current_symlink —
    `modulo start` runs the same repair): an operator's bricked install
    self-heals instead of dead-ending on an internal Python function.
    """
    current_link = install_root / "current"
    if not current_link.is_symlink():
        raise UpgradeError(
            f"no native install to upgrade under {install_root} (no resolvable `current`) - "
            "install.sh sets one up; a populated data dir alone is not an install"
        )
    try:
        resolved_target = current_link.resolve(strict=True)
    except OSError:
        repair_state = repair_current_symlink(install_root)
        if repair_state is None:
            raise UpgradeError(
                f"the `current` symlink under {install_root} is DANGLING and could not be repaired "
                "(no version dir survives) - reinstall:  bash scripts/install.sh"
            ) from None
        _log.info("upgrade.current_symlink_repaired version=%s", repair_state)
        try:
            resolved_target = current_link.resolve(strict=True)
        except OSError as exc:
            raise UpgradeError(f"the repaired `current` symlink still does not resolve: {exc}") from exc
    if not resolved_target.is_dir():
        raise UpgradeError(f"the `current` symlink resolves to a non-directory ({resolved_target})")
    return resolved_target


def assert_upgrade_platform() -> None:
    import sys

    if sys.platform == "win32":
        raise UpgradeError(
            "the full modulo upgrade flow is not implemented on Windows yet (TODO(P3)): "
            "the versioned-dir/current-symlink swap is a POSIX symlink primitive today"
        )
    if sys.platform == "darwin":
        raise UpgradeError("the full modulo upgrade flow is the TODO(P2) macOS launchd seam (FAR-678)")


def perform_upgrade(
    data_dir: Path,
    *,
    install_root: Path,
    target_version: str | None = None,
    from_file: Path | None = None,
    skip_backup: bool = False,
    fetch: Callable[[str, Path], None] | None = None,
    boot: Callable[..., int] | None = None,
    now: Callable[[], float] = time.time,
) -> UpgradeResult:
    """The end-to-end `modulo upgrade` flow (FAR-675, ADR 031 Decision 7).

    Stage order (destructive last):

    1. Fetch the target bundle (or point --from-file at a local tarball);
       SIGNATURE-verify the release manifest (current+next trust store);
       verify the tarball sha256 against it BEFORE extraction.
    2. Pre-flight refusals BEFORE the swap: PG major vs PG_VERSION (hard,
       never auto-initdb - the manual pg_upgrade remediation), downgrade
       (DB revision ahead of the target head), quantified disk (incl. dump
       headroom).
    3. The ENFORCED pre-upgrade pg_dump (--skip-backup is the loud flag; a
       verified snapshot is still required, else the flow refuses).
    4. The service-aware stop (the FAR-674 systemd user unit when
       installed, else the foreground lock holder); then a live-process
       exe/cwd refusal naming every PID under the incumbent version dir.
    5. The atomic `current` symlink swap (POSIX rename(2) - Windows
       refuses at the seam, TODO(P3)) + upgrade.json written BEFORE the
       boot.
    6. Boot the new bundle's lifespan migrations via `launcher start`,
       gated on /healthz. After a HEALTHY gate the FAR-674 systemd unit is
       restarted (best-effort; without the unit the flow leaves the stack
       stopped and the refusal/runbook wording says so) - the boot child's
       own process group was torn down by the /healthz gate, so the resting
       state is deliberately the systemd-managed service, never an orphaned
       boot child.
    7. On failure: the hard refusal prints the snapshot path + the EXACT
       manual restore command restoring BOTH binary AND data (explicit
       forward-migrated-schema wording). NO automatic restore runs (the
       fast-follow design is recorded in ADR 031 Decision 7 - the message
       references it). Every post-stop abort restarts the stopped unit
       best-effort: a refusal must never leave the stack DARK.
    8. Retention (the last 2 version dirs; sweep caches; never the
       state.json-referenced incumbent or the re-run .prev-<ts> copies).
    """
    assert_upgrade_platform()
    if target_version is not None:
        target_version = _strip_bundle_prefix(target_version)
    reserve_target = _resolve_current_target(install_root)
    previous_version = reserve_target.name
    scrub_os_environment()

    with tempfile.TemporaryDirectory(prefix="modulo-upgrade-") as temp:
        staging_dir = Path(temp)
        if from_file is not None:
            tarball = Path(from_file)
            if not tarball.is_file():
                raise UpgradeError(f"--from-file: no such file: {from_file}")
            version = _strip_bundle_prefix(
                tarball.name.removeprefix("modulo-")
                .removesuffix("-linux-amd64.tar.gz")
                .removesuffix("-linux-arm64.tar.gz")
            )
            manifest_file = tarball.parent / _RELEASE_MANIFEST_NAME
            signature_file = tarball.parent / f"{_RELEASE_MANIFEST_NAME}.sig"
            if not (manifest_file.is_file() and signature_file.is_file()):
                raise UpgradeError(
                    "--from-file requires the SIGNED manifest beside the tarball: "
                    f"{manifest_file} (+ .sig) - the offline escape hatch verifies against "
                    "the SAME embedded trust store (ADR 031 Decision 3)"
                )
        else:
            if target_version is None:  # pragma: no cover - CLI-gated
                raise UpgradeError("a pinned target version is REQUIRED (e.g. bundle-v0.1.0)")
            downloads = fetch_release_assets(target_version, staging_dir / "downloads", fetch=fetch)
            version = downloads.version
            tarball = downloads.tarball
            manifest_file = downloads.manifest
            signature_file = downloads.signature

        release_manifest = _verify_release_stage(manifest_file, signature_file, tarball, version)

        _log.info("upgrade.release_manifest_verified release=%s", release_manifest.release)
        bundle_dir = extract_and_verify_bundle(tarball, staging_dir / "extracted")
        # Per-binary manifest check (post-extraction): the manifest covers the
        # major bundle binaries too - release artifacts get checked against the
        # files as they sit in the REAL bundle, not just the tarball.
        _verify_extracted_bundle_artifacts(bundle_dir, release_manifest, tarball.name)

        # -- Pre-flight refusals (BEFORE anything version-specific) -------
        _check_pg_major(release_manifest.components, data_dir)
        if skip_backup:
            # A stopped stack cannot be dumped: the LIVE probe answers the
            # DB side instead (failing blind when it cannot - see
            # check_no_downgrade; "unknown" is never pass-through).
            snapshot = None
            snapshot_dir = None
            db_heads = _alembic_heads()
        else:
            snapshot = pre_upgrade_dump(data_dir, bin_dir=_prior_pg_bin(reserve_target))
            snapshot_dir = snapshot.directory
            db_heads = _snapshot_schema_versions(snapshot_dir)
        check_no_downgrade(db_heads, snapshot_dir, bundle_dir)
        _quantified_disk_preflight(tarball, data_dir)

        # -- The DESTRUCTIVE phase (stages 4-8) ---------------------------
        stop_running_stack(data_dir)
        try:
            no_live_process_inside(reserve_target)
            assert_not_held(data_dir)
            _target_dir, prior_dir = _move_into_place(bundle_dir, install_root, version)
            atomic_symlink_swap(install_root / "current", f"versions/{version}")
        except UpgradeError:
            _restart_unit_best_effort(data_dir, why="post-stop abort: restoring the stopped service")
            raise
        marker_path = write_upgrade_marker(
            data_dir,
            previous_version=previous_version,
            version=version,
            pre_upgrade_snapshot=snapshot_dir,
            upgraded_at=now(),
        )
        _log.info("upgrade.marker_written path=%s", marker_path)
        boot_runner = boot or run_boot
        prior_link_target = prior_dir.name if prior_dir is not None else None
        try:
            boot_runner(_bundled_boot_argv(install_root, version, data_dir), api_port=_api_port_of(data_dir))
        except UpgradeError as exc:
            raise UpgradeError(
                f"{exc}\n\n"
                + restore_guidance(
                    snapshot=snapshot_dir,
                    previous_version=previous_version,
                    install_root=install_root,
                    data_dir=data_dir,
                    prior_link_target=prior_link_target,
                )
            ) from exc
        _restart_unit_best_effort(data_dir, why="healthy post-upgrade boot gate passed: restoring the service")
        pruned = prune_versions(install_root, current_target=reserve_target)
        pruned += sweep_upgrade_caches(install_root)

    return UpgradeResult(
        previous_version=previous_version,
        version=version,
        snapshot=snapshot_dir,
        marker_path=marker_path,
        pruned=pruned,
    )


def _verify_release_stage(manifest_file: Path, signature_file: Path, tarball: Path, version: str) -> ReleaseManifest:
    """Signature-verify the manifest + tarball sha256; assert the release tag.

    A ManifestSecurityError (tampered/garbled/missing) surfaces as
    :class:`UpgradeError` - the CLI's error handling turns THAT into a clean
    ClickException, never a raw traceback. The manifest's release field must
    equal the requested bundle tag (never sign one version and ship another).
    """
    expected_release = f"bundle-v{version}"
    try:
        release_manifest = manifest_module.verify_manifest(
            manifest_file.read_bytes(), manifest_module.read_signature_file(signature_file)
        )
        expected_tarball_sha = release_manifest.checksum_for(tarball.name)
        if release_manifest.release != expected_release:
            raise UpgradeError(
                f"the signed release manifest describes '{release_manifest.release}', not the "
                f"requested bundle {expected_release} - refusing the cross-version mismatch"
            )
        tarball_sha = _sha256_file(tarball)
    except ManifestSecurityError as exc:
        raise UpgradeError(f"release manifest verification FAILED: {exc}") from exc
    if tarball_sha != expected_tarball_sha:
        raise UpgradeError(
            f"the signed release manifest sha256 MISMATCH for {tarball.name}: expected "
            f"{expected_tarball_sha}, got {tarball_sha} - do not use this download; report "
            "it at https://github.com/farnalabs/modulo/issues"
        )
    return release_manifest


def _verify_extracted_bundle_artifacts(bundle_dir: Path, release_manifest: ReleaseManifest, tarball_name: str) -> None:
    """Post-extraction per-binary manifest gate.

    The signed manifest covers the major bundled binaries (pg/postgres,
    redis-server, ...) IN the bundle: after extraction every one is checked
    in the real layout. The tarball entry is the pre-extraction check that
    already ran - it is skipped here (the tarball is not a bundle member).
    """
    try:
        bundle_side_manifest = replace(
            release_manifest,
            artifact_checksums={
                name: entry for name, entry in release_manifest.artifact_checksums.items() if name != tarball_name
            },
        )
        manifest_module.verify_artifacts(bundle_dir, bundle_side_manifest)
    except manifest_module.ManifestSecurityError as exc:
        raise UpgradeError(f"bundle-side manifest verification FAILED after extraction: {exc}") from exc


if __name__ == "__main__":
    sys.exit(main())
