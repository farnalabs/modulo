"""modulo backup/restore: CLI for self-hosted backup and disaster recovery.

Usage:
  modulo backup [--db-url <url>] [--output-dir <path>] [--data-dir <path>] [--include-secrets]
  modulo restore <backup-dir> [--db-url <url>] [--yes] [--previous-fernet-key <key>] [--dry-run]
               [--data-dir <path>] [--replace-cluster]

Data-safety semantics (FAR-672 / ADR 031 Decision 6/7):

* The backup manifest is VERSIONED (``manifest_version``). Legacy archives
  (no ``manifest_version`` field) restore fine; an archive written by a
  NEWER Modulo than this server supports is refused.
* Launcher data-dir artefacts ride along when ``--data-dir`` is given:
  the credential-free ``state.json`` always; the 0600 secrets file and the
  credential-bearing conf profile ONLY with the explicit opt-in flag
  ``--include-secrets`` (loud warning); the TLS keypair when present.
  Redis/SAQ queue data is ephemeral and EXCLUDED by design — documented in
  the manifest.
* The default archive contains no cleartext FERNET_KEY/SECRET_KEY-class
  material and no launcher secrets: safe to move through ordinary storage.
  ``--include-secrets`` produces a complete DR/machine-migration archive.
* A backup against a data dir takes the exclusive data-dir lock (refusing
  with the holder PID while a start is running) and persists the
  last-successful-backup timestamp in the HMAC'd state.json
  (``last_backup_at``, forward-compatible optional field).
* Restore verifies EVERYTHING before it touches anything (manifest version,
  instance identity, disk pre-flight, checksums) and refuses to run against
  a data dir whose launcher is still running.
* Instance identity: populated-instance identifier (state.json HMAC) must
  match the archive; a mismatch refuses unless ``--replace-cluster`` is
  given (with a loud confirmation printing both identifiers). Fresh /
  uninitialized targets are exempt — disaster recovery and machine
  migration must keep working.
* After the database restore, the promoted bootstrap/bootstrap_role posture
  path re-runs, and the Postgres collation-version table is checked:
  an INCOMPATIBLE collation-version drift (different libc family, e.g.
  the compose musl -> native glibc path) HARD REFUSES the restore, while
  a compatible-but-older same-family drift warns and proceeds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import subprocess  # nosec B404 -- subprocess is used only to shell out to pg_dump/psql with a hardcoded argv list (never shell=True, never user-supplied commands); see _run_pg_dump/_run_psql
import sys
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row

from modulo.cli.apply import register_apply
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# Repeated error message and backup-artifact filenames (S1192). Constants are
# pure aliases — the exported file names are part of the on-disk backup layout.
_ERR_PSYCOPG_UNAVAILABLE = "psycopg library is not available"
_DB_SQL_FILE = "database.sql"
_BLOBS_JSON_FILE = "checkpoint_blobs.json"
_CHECKPOINTS_JSON_FILE = "checkpoints.json"
_WRITES_JSON_FILE = "checkpoint_writes.json"
_CREDS_JSON_FILE = "credentials_references.json"

# A restore takes a pre-restore safety dump of the TARGET first (or the
# operator passes --no-safety-dump explicitly): a mid-import failure must
# leave the previous cluster restorable.
_SAFETY_DUMP_PREFIX = "pre-restore-dump-"

# Versioned manifest (FAR-672): 2 adds the launcher-snapshot section. The
# restore path accepts 1 (legacy) and 2; anything newer is refused.
MANIFEST_VERSION = 2
_BACKUP_INFO_FILE = "backup-info.json"

# Launcher data-dir artefact names (ADR 031 Decision 6 layout). These are the
# on-disk contract names inside the data dir AND the backup layout.
_LAUNCHER_STATE_FILE = "state.json"
_LAUNCHER_SECRETS_FILE = "secrets.json"
_LAUNCHER_CONF_FILE = "config.env"
_TLS_DIRNAME = "tls"

# A backup placed inside a cloud-synced folder risks silent corruption (sync
# churn mid-dump) and unencrypted storage of a credential-free-but-sensitive
# archive. Detected by comparing each path COMPONENT (case-folded, exact
# equality) against the known vendor roots — substring matching would
# false-positive on ordinary names like "dropbox-migrations-backup-tmp".
_CLOUD_SYNC_VENDOR_ROOTS = frozenset({"dropbox", "onedrive", "icloud", "googledrive"})

# The launcher-owned public surface (consumed by modulo.cli.main; vulture's
# dead-code gate special-cases __all__).
__all__ = ["MANIFEST_VERSION", "backup", "cli", "restore"]


# ── URL helpers ──────────────────────────────────────────────────────────────


def _resolve_url(db_url: str | None) -> str:
    """Resolve a raw PostgreSQL connection string from --db-url or settings."""
    raw = db_url or get_settings().database_url
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix) :]
    return raw


# ── Fernet helpers ───────────────────────────────────────────────────────────


def _fernet_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── JSON serialisation helpers ────────────────────────────────────────────────


def _serialise_for_json(obj: Any) -> Any:
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _serialise_export_row(row: dict[str, Any]) -> dict[str, Any]:
    serialised: dict[str, Any] = {}
    for key, val in row.items():
        if isinstance(val, uuid.UUID):
            serialised[key] = str(val)
        elif isinstance(val, bytes | memoryview):
            serialised[key] = bytes(val).hex()
        elif isinstance(val, datetime):
            serialised[key] = val.isoformat()
        else:
            serialised[key] = val
    return serialised


def _parse_org_id(raw: str | None) -> uuid.UUID | None:
    if raw:
        try:
            return uuid.UUID(raw)
        except (ValueError, TypeError):
            raise RuntimeError(f"Invalid organisation_id: {raw!r}") from None
    return None


def _write_json(path: Path, data: Any) -> None:
    """Atomically-tight JSON write: the file is CREATED 0600 (never briefly
    exposed at umask mode for the whole serialisation), Windows no-op."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=_serialise_for_json)


# ── Metadata helpers ─────────────────────────────────────────────────────────


def _get_backend_dir() -> Path:
    resolved = Path(__file__).resolve()
    for candidate in (resolved.parent.parent.parent, resolved.parent.parent.parent.parent):
        if (candidate / "pyproject.toml").exists() and (candidate / "alembic.ini").exists():
            return candidate
    return resolved.parent.parent.parent.parent


def _get_schema_versions() -> list[str]:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = _get_backend_dir()
        alembic_ini = backend_dir / "alembic.ini"
        if not alembic_ini.exists():
            return ["unknown"]
        cfg = Config(str(alembic_ini))
        script = ScriptDirectory.from_config(cfg)
        return sorted(script.get_heads())
    except Exception as exc:
        _log.warning("Failed to read schema versions: %s", exc)
        return ["unknown"]


def _get_db_version(raw_url: str) -> str:
    if psycopg is None:
        return "unknown"
    try:
        with psycopg.connect(raw_url, connect_timeout=5) as conn:
            row = conn.execute("SELECT version()").fetchone()
            return row[0] if row else "unknown"
    except Exception as exc:
        _log.warning("Failed to read DB version: %s", exc)
        return "unknown"


# ── pg_dump / psql helpers ────────────────────────────────────────────────────


def _check_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Required system tool '{name}' not found on PATH. Install PostgreSQL client tools.")


def _decredential_url(raw_url: str) -> tuple[str, str | None]:
    """Split the password out of a PostgreSQL URL.

    Returns ``(url_without_password, password)``. pg_dump/psql must never
    receive the password as an argv element: argv is world-readable via
    ``/proc/<pid>/cmdline`` while the child runs. The password travels in
    the child-scoped ``PGPASSWORD`` env var instead (see _pg_child_env).
    """
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
    clean = parsed._replace(netloc=netloc)
    return urllib.parse.urlunsplit(clean), urllib.parse.unquote(password)


def _pg_child_env(password: str | None) -> dict[str, str]:
    """Child-scoped env for pg_dump/psql: the password ONLY as PGPASSWORD."""
    env = dict(os.environ)
    env.pop("PGPASSWORD", None)
    if password is not None:
        env["PGPASSWORD"] = password
    return env


def _run_pg_dump(raw_url: str, output: Path, timeout: int = 300) -> None:
    _check_tool("pg_dump")
    url_without_password, password = _decredential_url(raw_url)
    cmd = [
        "pg_dump",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--format=plain",
        url_without_password,
    ]
    # The dump file is CREATED 0600 (touch) so a mid-dump SIGKILL never
    # leaves the SQL dump readable at umask mode; the subsequent open("wb")
    # keeps the already-created 0600 mode.
    output.touch(mode=0o600)
    try:
        with output.open("wb") as f:
            result = subprocess.run(  # nosec B603 -- cmd is a hardcoded argv list, never shell=True, never user input  # noqa: S603
                cmd, stdout=f, stderr=subprocess.PIPE, check=False, timeout=timeout, env=_pg_child_env(password)
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pg_dump timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()}")


def _run_psql(raw_url: str, input_path: Path, timeout: int = 600) -> None:
    _check_tool("psql")
    url_without_password, password = _decredential_url(raw_url)
    # -1 (single transaction): the --clean dump replays its DROPs and CREATEs
    # atomically — a mid-import failure leaves the target database exactly as
    # it was, not half-dropped. Combined with the pre-restore safety dump
    # (see _take_safety_dump) a failed restore stays recoverable.
    cmd = ["psql", "-q", "-1", "-v", "ON_ERROR_STOP=1", url_without_password]
    try:
        with input_path.open("rb") as f:
            result = subprocess.run(  # nosec B603 -- cmd is a hardcoded argv list with trusted input, never shell=True  # noqa: S603
                cmd, stdin=f, capture_output=True, check=False, timeout=timeout, env=_pg_child_env(password)
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"psql restore timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"psql restore failed: {result.stderr.decode(errors='replace').strip()}")


def _take_safety_dump(raw_url: str, timeout: int = 300) -> Path | None:
    """pg_dump the CURRENT target before it is wiped (restore safety net).

    A mid-import psql failure must leave the pre-restore cluster restorable:
    the dump lands in a private-mode sibling directory and its path is
    reported. Any dump failure refuses the restore (the operator may pass
    --no-safety-dump explicitly to proceed without one). The ONLY tolerated
    degradation is pg_dump not being installed at all — in which case a loud
    warning is issued and the restore continues bare-backed: psql needs the
    dump replayed anyway, so a pg_dump-less machine's restore dies on the
    very next step without ever touching the database.
    """
    if shutil.which("pg_dump") is None:
        click.echo(
            "WARNING: pg_dump is not installed — proceeding WITHOUT a pre-restore safety dump. "
            "The restore will fail on the psql step before touching the database; install the "
            "PostgreSQL client tools for a full safety net (or pass --no-safety-dump to silence this).",
            err=True,
        )
        return None
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision  # noqa: S311
    safety_dir = Path(f"./{_SAFETY_DUMP_PREFIX}{ts}-{suffix}")
    while safety_dir.exists():
        suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision  # noqa: S311
        safety_dir = Path(f"./{_SAFETY_DUMP_PREFIX}{ts}-{suffix}")
    try:
        safety_dir.mkdir()
        _run_pg_dump(raw_url, safety_dir / _DB_SQL_FILE, timeout=timeout)
    except Exception as exc:
        shutil.rmtree(safety_dir, ignore_errors=True)
        raise click.ClickException(
            "Taking the pre-restore safety dump FAILED — refusing to wipe the current data (nothing was restored). "
            "Fix the dump failure, or pass --no-safety-dump to explicitly proceed without a safety net. "
            f"Original error: {exc}"
        ) from exc
    click.echo(f"Pre-restore safety dump taken: {safety_dir} (the previous cluster stays restorable)")
    return safety_dir


# ── Sync data export (via psycopg) ────────────────────────────────────────────


def _export_checkpoint_blobs_sync(raw_url: str) -> list[dict[str, Any]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with psycopg.connect(raw_url, row_factory=dict_row, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM checkpoint_blobs ORDER BY organisation_id, thread_id, checkpoint_ns, channel, version"
        )
        return [_serialise_export_row(row) for row in cur]


def _export_checkpoints_sync(raw_url: str) -> list[dict[str, Any]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with psycopg.connect(raw_url, row_factory=dict_row, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM checkpoints ORDER BY organisation_id, thread_id, checkpoint_ns, checkpoint_id")
        return [_serialise_export_row(row) for row in cur]


def _export_checkpoint_writes_sync(raw_url: str) -> list[dict[str, Any]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with psycopg.connect(raw_url, row_factory=dict_row, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM checkpoint_writes "
            "ORDER BY organisation_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx"
        )
        return [_serialise_export_row(row) for row in cur]


_CREDENTIALS_TABLES: list[str] = ["connector_instances", "model_backends"]


def _export_credentials_references_sync(raw_url: str) -> dict[str, list[dict[str, Any]]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    result: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(raw_url, row_factory=dict_row, connect_timeout=10) as conn:
        for table in _CREDENTIALS_TABLES:
            rows: list[dict[str, Any]] = []
            with conn.cursor() as cur:
                sql = f"SELECT id, organisation_id, name, credentials_ciphertext FROM {table} ORDER BY id"  # nosec B608  # noqa: S608 -- table comes from a fixed whitelist
                cur.execute(sql)
                for row in cur:
                    org_id = row.get("organisation_id")
                    row["id"] = str(row["id"])
                    row["organisation_id"] = str(org_id) if org_id is not None else None
                    if isinstance(row.get("credentials_ciphertext"), bytes | memoryview):
                        ct = bytes(row["credentials_ciphertext"])
                        row["credentials_ciphertext"] = ct.hex()  # nosemgrep: credential-not-in-state
                    rows.append(row)
            result[table] = rows
    return result


def _restore_checkpoint_blobs_sync(raw_url: str, blobs: list[dict[str, Any]]) -> int:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with psycopg.connect(raw_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoint_blobs CASCADE")
            for row in blobs:
                blob: bytes | None = None
                raw_blob = row.get("blob")
                if raw_blob is not None:
                    blob = bytes.fromhex(raw_blob) if raw_blob else b""
                org_uuid = _parse_org_id(row.get("organisation_id"))
                cur.execute(
                    "INSERT INTO checkpoint_blobs "
                    "(organisation_id, thread_id, checkpoint_ns, channel, version, type, blob) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        org_uuid,
                        row.get("thread_id"),
                        row.get("checkpoint_ns"),
                        row.get("channel"),
                        row.get("version"),
                        row.get("type"),
                        blob,
                    ),
                )
        conn.commit()
    return len(blobs)


def _restore_checkpoints_sync(raw_url: str, checkpoints: list[dict[str, Any]]) -> int:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with psycopg.connect(raw_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoints CASCADE")
            for row in checkpoints:
                org_uuid = _parse_org_id(row.get("organisation_id"))
                cur.execute(
                    "INSERT INTO checkpoints "
                    "(organisation_id, thread_id, checkpoint_ns, checkpoint_id, "
                    " parent_checkpoint_id, checkpoint, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        org_uuid,
                        row.get("thread_id"),
                        row.get("checkpoint_ns"),
                        row.get("checkpoint_id"),
                        row.get("parent_checkpoint_id"),
                        row.get("checkpoint"),
                        row.get("metadata"),
                    ),
                )
        conn.commit()
    return len(checkpoints)


def _restore_checkpoint_writes_sync(raw_url: str, writes: list[dict[str, Any]]) -> int:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with psycopg.connect(raw_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoint_writes CASCADE")
            for row in writes:
                blob: bytes | None = None
                raw_blob = row.get("blob")
                if raw_blob is not None:
                    blob = bytes.fromhex(raw_blob) if raw_blob else b""
                org_uuid = _parse_org_id(row.get("organisation_id"))
                cur.execute(
                    "INSERT INTO checkpoint_writes "
                    "(organisation_id, thread_id, checkpoint_ns, checkpoint_id, "
                    " task_id, idx, channel, type, blob) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        org_uuid,
                        row.get("thread_id"),
                        row.get("checkpoint_ns"),
                        row.get("checkpoint_id"),
                        row.get("task_id"),
                        row.get("idx"),
                        row.get("channel"),
                        row.get("type"),
                        blob,
                    ),
                )
        conn.commit()
    return len(writes)


def _re_encrypt_credentials_sync(
    raw_url: str,
    creds: dict[str, list[dict[str, Any]]],
    old_fernet_key: str,
    new_fernet_key: str,
) -> dict[str, int]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    old_fernet = Fernet(old_fernet_key.encode())
    new_fernet = Fernet(new_fernet_key.encode())
    counts: dict[str, int] = {}

    with psycopg.connect(raw_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for table, rows in creds.items():
                if table not in _CREDENTIALS_TABLES:
                    _log.warning("Skipping unknown credentials table: %s", table)
                    continue
                rekeyed = 0
                for row in rows:
                    hex_ct = row.get("credentials_ciphertext", "")
                    if not hex_ct:
                        continue
                    old_ct = bytes.fromhex(hex_ct)
                    try:
                        plaintext = old_fernet.decrypt(old_ct)
                    except InvalidToken as exc:
                        raise RuntimeError(
                            f"Failed to decrypt {table} row {row.get('id', '?')}: --previous-fernet-key may be wrong"
                        ) from exc
                    new_ct = new_fernet.encrypt(plaintext)
                    try:
                        row_id = uuid.UUID(row["id"])
                    except (ValueError, TypeError):
                        _log.warning("Invalid UUID in credentials row: %s", row.get("id", "?"))
                        continue
                    cur.execute(
                        f"UPDATE {table} SET credentials_ciphertext = %s WHERE id = %s",  # nosec B608  # noqa: S608 -- table is whitelist-validated
                        (new_ct, row_id),
                    )
                    rekeyed += 1
                counts[table] = rekeyed
        conn.commit()
    return counts


# ── Size helper ──────────────────────────────────────────────────────────────


def _print_size(backup_dir: Path) -> None:
    try:
        total = sum(f.stat().st_size for f in backup_dir.rglob("*") if f.is_file())
    except OSError as exc:
        _log.warning("Could not compute backup size: %s", exc)
        return
    click.echo(f"Total size: {_human_size(total)}")


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── Data-safety helpers (FAR-672 / ADR 031 Decisions 6/7) ─────────────────────


def _warn_cloud_sync_output(backup_dir: Path) -> None:
    """Loud warning when the archive lands inside a cloud-synced folder.

    Dropbox/OneDrive/iCloud sync churn can corrupt a dump mid-write and
    syncs the archive (however secret-free) off the machine automatically.
    """
    lowered_parts = [part.casefold() for part in backup_dir.parts]
    # Component equality (case-folded): "Dropbox", "Dropbox (2)"-style vendor
    # roots are matched by name; ordinary names that merely CONTAIN the word
    # do not warn.
    hit = sorted(root for root in _CLOUD_SYNC_VENDOR_ROOTS if root in lowered_parts)
    if hit:
        click.echo(
            f"WARNING: backup target {backup_dir} is inside a cloud-synced folder ({', '.join(hit)}). "
            "Sync churn can corrupt the dump mid-write and the archive leaves the machine automatically. "
            "Back up to a local directory and copy the finished archive explicitly instead.",
            err=True,
        )


def _lock_artifact_mode(backup_dir: Path, artifact_names: list[str], *, user_specified_dir: bool) -> None:
    """Tighten the archive artifacts the BACKUP created to owner-only.

    Directories are chmod 0700 — the e(xecute) bit is the DIRECTORY search
    bit on POSIX: a directory at 0600 is unlistable AND unsearchable, so any
    file inside it (e.g. the TLS keypair under ``tls/``) becomes unresolvable
    and restoring it fails. Files are 0600. Each entry is tightened
    independently: one unchmoddable entry logs a warning and continues
    instead of aborting the sweep. Only artefacts THIS backup produced are
    touched — a user-supplied ``--output-dir`` root it did not create keeps
    its own mode (and contents).
    TODO(P3): Windows ACL hardening (chmod is a silent no-op there).
    """
    if os.name != "posix":
        return
    targets: list[Path] = []
    if not user_specified_dir:
        targets.append(backup_dir)
    targets.extend(backup_dir / name for name in artifact_names)
    for path in targets:
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError as exc:
            _log.warning("backup_artifact_mode_failed path=%s error=%s", path, exc)


def _acquire_data_lock(data_dir: Path, *, mode: str) -> Any:
    """Take the exclusive data-dir lock, refusing while a launcher runs.

    A holder is named in the raised error (PIDs are part of the
    DataDirLockError message), so a start in flight blocks backup/restore
    with an actionable message instead of racing it.
    """
    from modulo.launcher.supervisor import DataDirLock, DataDirLockError

    lock = DataDirLock(data_dir, mode=mode)
    try:
        lock.acquire()
    except DataDirLockError as exc:
        raise click.ClickException(str(exc)) from exc
    return lock


def _read_state_mac(state_path: Path) -> str | None:
    """Read the HMAC identity fingerprint recorded in a state.json envelope.

    The envelope's ``mac`` is an HMAC over the (credential-free) state
    payload keyed by the instance's state_hmac_key — a stable, non-secret
    fingerprint that pins an instance across boots (the key and ports do
    not change). Two machines' instances can never share it.
    """
    try:
        envelope = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"state.json at {state_path} is unreadable: {exc}") from exc
    if not isinstance(envelope, dict):
        return None
    mac = envelope.get("mac")
    return mac if isinstance(mac, str) and mac else None


def _launcher_snapshot_files(
    data_dir: Path, backup_dir: Path, include_secrets: bool
) -> tuple[dict[str, Any], list[str]]:
    """Copy the launcher data-dir artefacts into the archive; describe them.

    Included unconditionally (when present): ``state.json`` — credential-free
    by contract. Included ONLY with ``--include-secrets``: the 0600 secrets
    file and the credential-bearing conf profile (both carry the generated
    service passwords; a default archive must never contain them). TLS
    keypair files ride along when the data dir has a ``tls/`` directory —
    NOTE in the manifest: this includes the server PRIVATE KEY even in a
    default archive (the credential-free claim is about launcher/FERNET
    secrets, not the TLS key; excluded by not pointing --data-dir at it).

    Returns ``(manifest_section, checksums)`` — checksums over every file
    copied here so the restore's verification loop covers the ride-alongs.
    The caller must snapshot state.json AFTER ``_record_last_backup``
    (ordering locked by test) so the archived file + its ``state_mac``
    describe the data dir's FINAL on-disk state.
    """
    section: dict[str, Any] = {
        "state_json_included": False,
        "state_mac": None,
        "conf_profile": {"included": False, "note": "excluded by default (carries generated credentials)"},
        "secrets_file": {"included": False, "note": "excluded by default (generated credentials)"},
        "tls_files": [],
        "tls_note": (
            "Rides along when the data dir has tls/ — includes the server PRIVATE KEY. "
            "Point --data-dir at a dir without tls/ (or leave --data-dir out) to keep TLS "
            "material off the archive."
        ),
        "ephemeral": {
            "redis_saq_queue_data": (
                "EXCLUDED by design — Redis/SAQ queue data is ephemeral (ADR 031 Decision 6); "
                "queued jobs do not survive a backup/restore cycle"
            )
        },
    }
    checksums: list[str] = []
    state_path = data_dir / _LAUNCHER_STATE_FILE
    if state_path.exists():
        shutil.copy2(state_path, backup_dir / _LAUNCHER_STATE_FILE)
        section["state_json_included"] = True
        checksums.append(_LAUNCHER_STATE_FILE)
        try:
            section["state_mac"] = _read_state_mac(state_path)
        except RuntimeError as exc:
            _log.warning("backup_state_identity_unreadable path=%s error=%s", state_path, exc)
    secrets_path = data_dir / _LAUNCHER_SECRETS_FILE
    conf_path = data_dir / _LAUNCHER_CONF_FILE
    if include_secrets:
        if secrets_path.exists():
            shutil.copy2(secrets_path, backup_dir / _LAUNCHER_SECRETS_FILE)
            section["secrets_file"]["included"] = True
            checksums.append(_LAUNCHER_SECRETS_FILE)
        if conf_path.exists():
            shutil.copy2(conf_path, backup_dir / _LAUNCHER_CONF_FILE)
            section["conf_profile"]["included"] = True
            checksums.append(_LAUNCHER_CONF_FILE)
        # The loud warning fires ONLY when at least one secret-bearing file
        # was actually copied — a --include-secrets run over a data dir with
        # no secrets/conf on disk stayed credential-free.
        if section["secrets_file"]["included"] or section["conf_profile"]["included"]:
            click.echo(
                "WARNING: --include-secrets produced a FULL-fidelity archive that contains the launcher "
                "secrets file and the credential-bearing conf profile (generated Postgres/Redis passwords "
                "and the state HMAC key). Protect the archive accordingly — anyone holding it gains full "
                "access to the bundled services. A default archive excludes all of this.",
                err=True,
            )
    tls_dir = data_dir / _TLS_DIRNAME
    if tls_dir.is_dir():
        target_tls = backup_dir / _TLS_DIRNAME
        target_tls.mkdir(exist_ok=True)
        for tls_file in sorted(tls_dir.iterdir()):
            if tls_file.is_file():
                shutil.copy2(tls_file, target_tls / tls_file.name)
                section["tls_files"].append(f"{_TLS_DIRNAME}/{tls_file.name}")
                checksums.append(f"{_TLS_DIRNAME}/{tls_file.name}")
    return section, checksums


def _record_last_backup(data_dir: Path, timestamp: str) -> None:
    """Persist the last-successful-backup timestamp into the HMAC'd state.json.

    The field is optional and forward-compatible: a data dir without a
    state.json (or without readable secrets) simply skips the record with a
    logged note — the database backup itself is never at risk from here.
    """
    from modulo.launcher.state import LauncherState, load_state, save_state

    state_path = data_dir / _LAUNCHER_STATE_FILE
    if not state_path.exists():
        _log.info("backup_timestamp_skipped (no state.json) data_dir=%s", data_dir)
        return
    try:
        secrets = json.loads((data_dir / _LAUNCHER_SECRETS_FILE).read_text(encoding="utf-8"))
        hmac_key = bytes.fromhex(secrets["state_hmac_key"])
        current = load_state(state_path, hmac_key)
        updated = LauncherState(
            postgres_port=current.postgres_port,
            redis_port=current.redis_port,
            api_port=current.api_port,
            schema_version=current.schema_version,
            last_backup_at=timestamp,
        )
        save_state(updated, state_path, hmac_key)
        _log.info("backup_timestamp_recorded data_dir=%s at=%s", data_dir, timestamp)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Launcher state is an enhancement, never a backup blocker.
        _log.warning("backup_timestamp_not_recorded data_dir=%s error=%s", data_dir, exc)
    except Exception as exc:
        # Broad catch-all must never break the backup itself — but it is NOT
        # collapsed into the specific tuple above: an unknown failure here is
        # a bug and deserves the traceback in the log, at ERROR level.
        _log.error("backup_timestamp_not_recorded data_dir=%s error=%s", data_dir, exc)
        _log.exception("backup_timestamp_unexpected_failure data_dir=%s", data_dir)


def _disk_preflight(backup_dir: Path, data_dir: Path | None) -> None:
    """Refuse an overwrite the disk cannot hold — BEFORE anything is wiped.

    Rough guard: the restored dump needs about its own size again in
    temporary WAL/transaction space, so 2x the database.sql size is
    required free. Uses the target filesystem (the data dir when given,
    otherwise the archive's filesystem).
    """
    db_sql = backup_dir / _DB_SQL_FILE
    if not db_sql.exists():
        return
    try:
        needed = db_sql.stat().st_size * 2
    except OSError as exc:
        _log.warning("backup_preflight_size_failed path=%s error=%s", db_sql, exc)
        return
    target_fs = data_dir if data_dir is not None else backup_dir
    try:
        free = shutil.disk_usage(str(target_fs)).free
    except OSError as exc:
        _log.warning("backup_preflight_statfs_failed path=%s error=%s", target_fs, exc)
        return
    if free < needed:
        raise click.ClickException(
            f"Disk pre-flight FAILED: the restore needs ~2x the dump size free "
            f"({_human_size(needed)}) but only {_human_size(free)} is available on {target_fs}. "
            "Free space before restoring — refusing to wipe the current data."
        )


def _check_instance_identity(
    data_dir: Path,
    manifest: dict[str, Any],
    replace_cluster: bool,
) -> None:
    """Refuse to restore over a DIFFERENT populated instance (verify-before-wipe).

    The archive's identity fingerprint is the source data-dir state.json's
    HMAC (recorded in the manifest). A populated target carries its own
    fingerprint. Rules:
    * fresh / uninitialized target (no state.json) — EXEMPT (with a
      database-populated probe refusing — see _check_target_db_populated):
      disaster recovery and machine migration must keep working.
    * target state.json without an identity fingerprint — REFUSED (closes
      the both-sides-None bypass: a populated target whose state.json has
      no ``mac`` cannot be proven to be or not be the archived instance,
      and ``--yes`` must not be able to suppress verification).
    * manifest without a fingerprint (legacy archive) — cannot compare;
      warn and proceed.
    * mismatch — REFUSED unless ``--replace-cluster`` (interactive-terminal
      confirmation printing both identifiers; ``--yes`` cannot suppress
      that confirmation, and a piped/non-tty stdin is refused outright).
    """
    identity = manifest.get("launcher_snapshot", {})
    archive_mac = identity.get("state_mac") if isinstance(identity, dict) else None
    target_state = data_dir / _LAUNCHER_STATE_FILE
    if not target_state.exists():
        return  # fresh / uninitialized target — exempt (DR / migration path)
    try:
        target_mac = _read_state_mac(target_state)
    except RuntimeError as exc:
        raise click.ClickException(
            f"The target data dir has an UNREADABLE state.json ({exc}). Use a clean data dir "
            "instead — refusing to restore over a state whose integrity cannot be checked."
        ) from exc
    if target_mac is None:
        raise click.ClickException(
            "The target data dir carries a populated state.json WITHOUT an identity fingerprint — "
            "integrity cannot be verified against the archive. Refusing to restore. Re-run the "
            "launcher on that data dir or use a clean data dir."
        )
    if archive_mac is None:
        if isinstance(identity, dict) and identity.get("state_json_included"):
            raise click.ClickException(
                "The archive includes a state.json whose identity cannot be verified — refusing to "
                "restore over this data dir."
            )
        click.echo(
            "WARNING: legacy archive carries no instance identity — cannot verify this restore targets "
            "the backed-up installation. Verify manually that this is the intended target.",
            err=True,
        )
        return
    if target_mac == archive_mac:
        return
    if not replace_cluster:
        raise click.ClickException(
            "Instance identity MISMATCH — refusing to restore.\n"
            f"  Target instance: {target_mac}\n"
            f"  Archive instance: {archive_mac}\n"
            "The target data dir holds a DIFFERENT populated Modulo instance than the backup. "
            "Pass --replace-cluster to intentionally replace it (with a confirmation), or restore "
            "onto a clean data dir."
        )
    click.echo(
        "REPLACE CLUSTER CONFIRMATION — you are about to OVERWRITE a populated Modulo instance:\n"
        f"  Target instance (data dir): {target_mac}\n"
        f"  Archive instance (backup):  {archive_mac}\n"
        "All data in the target data dir's database will be replaced by the archive.",
        err=True,
    )
    if not _is_interactive_stdin():
        raise click.ClickException(
            "--replace-cluster requires an INTERACTIVE confirmation (it intentionally overwrites a "
            "populated instance) — a piped/non-interactive stdin cannot approve it. Run the command "
            "directly in a terminal."
        )
    click.confirm("Replace the target instance with the archived one?", abort=True)


def _is_interactive_stdin() -> bool:
    """True only for a real terminal stdin — a piped ``echo y |`` must NOT be
    able to approve the destructive --replace-cluster confirmation."""
    try:
        return sys.stdin is not None and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    except (OSError, ValueError):
        return False


def _restore_launcher_files(backup_dir: Path, data_dir: Path) -> list[str]:
    """Install the archive's launcher artefacts into the target data dir.

    Present only in archives written with --data-dir (and with
    --include-secrets for the credential-bearing ones). Copying them is what
    makes DR/machine-migration work: a fresh target adopts the archive's
    credentials and ports instead of generating orphaned ones on first boot.
    Returns the names copied (for the restore transcript).
    """
    copied: list[str] = []
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in (_LAUNCHER_STATE_FILE, _LAUNCHER_SECRETS_FILE, _LAUNCHER_CONF_FILE):
        source = backup_dir / name
        if source.exists():
            shutil.copy2(source, data_dir / name)
            copied.append(name)
    tls_dir = backup_dir / _TLS_DIRNAME
    if tls_dir.is_dir():
        target_tls = data_dir / _TLS_DIRNAME
        target_tls.mkdir(exist_ok=True)
        for tls_file in sorted(tls_dir.iterdir()):
            if tls_file.is_file():
                shutil.copy2(tls_file, target_tls / tls_file.name)
                copied.append(f"{_TLS_DIRNAME}/{tls_file.name}")
    return copied


def _check_manifest_version(manifest: dict[str, Any]) -> None:
    """Refuse archives written by a NEWER Modulo (versioned manifest, FAR-672)."""
    raw_version = manifest.get("manifest_version")
    if raw_version is None:
        return  # legacy archive — accepted
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise click.ClickException(f"backup manifest_version is not an integer: {raw_version!r}")
    if raw_version > MANIFEST_VERSION:
        raise click.ClickException(
            f"Refusing to restore: the archive's manifest_version {raw_version} is NEWER than this "
            f"Modulo supports (max {MANIFEST_VERSION}). Upgrade Modulo first, then restore."
        )
    if raw_version < 1:
        raise click.ClickException(f"Refusing to restore: invalid manifest_version {raw_version!r}.")


def _read_and_validate_manifest(backup_dir: Path) -> dict[str, Any]:
    """Read + shape-validate the archive manifest BEFORE anything is echoed.

    A hostile/corrupt manifest must never reach the echo path (an echoed
    wrong-typed field is a crash — TypeError on f-string join, or worse, an
    operator believing a number they never wrote). Validates: the file
    exists, parses, is a JSON OBJECT, and every field echoed later carries
    the right type. Folds in the version gate (manifest_version).
    """
    manifest_path = backup_dir / _BACKUP_INFO_FILE
    if not manifest_path.exists():
        raise click.ClickException(f"{_BACKUP_INFO_FILE} not found in {backup_dir}")
    try:
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"The backup manifest {manifest_path} is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise click.ClickException(
            f"The backup manifest {manifest_path} is not a JSON object — refusing to restore "
            "from an archive whose manifest shape cannot be trusted."
        )
    _check_manifest_version(manifest)
    for key in ("timestamp", "backup_type", "db_version"):
        if key in manifest and not isinstance(manifest[key], str):
            raise click.ClickException(f"backup manifest field {key} is not a string: {manifest[key]!r}")
    schema_versions = manifest.get("schema_versions", [])
    if not isinstance(schema_versions, list) or any(not isinstance(v, str) for v in schema_versions):
        raise click.ClickException(
            f"backup manifest field schema_versions is not a list of strings: {schema_versions!r}"
        )
    checksums = manifest.get("file_checksums")
    if checksums is not None and (
        not isinstance(checksums, dict)
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in checksums.items())
    ):
        raise click.ClickException("backup manifest field file_checksums is not a mapping of strings")
    if "fernet_key_hash" in manifest and not isinstance(manifest["fernet_key_hash"], str):
        raise click.ClickException(
            f"backup manifest field fernet_key_hash is not a string: {manifest['fernet_key_hash']!r}"
        )
    launcher = manifest.get("launcher_snapshot")
    if launcher is not None and not isinstance(launcher, dict):
        raise click.ClickException("backup manifest field launcher_snapshot is not an object")
    return manifest


def _check_schema_downgrade(manifest: dict[str, Any]) -> None:
    """Refuse restoring an archive written from a NEWER schema.

    The manifest records the alembic head(s) at backup time. A dump from a
    NEWER schema replayed into an OLDER binary's schema leaves the database
    one migration ahead (or in an unknown state); refuse BEFORE anything is
    wiped. Archives without a schema record (or with the 'unknown'
    placeholder) cannot be compared and are allowed with a warning.
    """
    if manifest.get("manifest_version") is None:
        # Legacy archives (pre-FAR-672) did not document schema_versions as
        # alembic heads — loud warning only; the hard gate applies to the
        # versioned manifests whose field is a genuine alembic-head record.
        click.echo(
            "WARNING: legacy archive carries no verifiable schema version — cannot check for a downgrade.",
            err=True,
        )
        return
    archive_versions = manifest.get("schema_versions") or []
    if not archive_versions or any(not isinstance(v, str) or v == "unknown" for v in archive_versions):
        click.echo("WARNING: archive carries no verifiable schema version — cannot check for a downgrade.", err=True)
        return
    current = _get_schema_versions()
    if any(not isinstance(v, str) or v == "unknown" for v in current):
        click.echo(
            f"WARNING: cannot read this installation's current schema version — the archive declares "
            f"{', '.join(archive_versions)}. Verify manually that this binary is not OLDER before restoring.",
            err=True,
        )
        return
    missing = sorted(set(archive_versions) - set(current))
    if missing:
        raise click.ClickException(
            f"Refusing to restore: the archive's schema ({', '.join(missing)}) is NEWER than this "
            f"installation speaks ({', '.join(current)}). Downgrading a database under an older binary "
            "corrupts the schema. Upgrade Modulo to the matching newer version first."
        )


def _db_has_modulo_tables(raw_url: str) -> bool:
    """Does the target database already hold application tables?

    The fresh-target identity exemption is keyed on state.json's ABSENCE —
    but a POPULATED database combined with a fresh/absent data dir must NOT
    be exempt: that is someone's production data. Probes for any base table
    in the public schema; conservative on error (True = treat as populated
    ⇒ require --replace-cluster — never silently exempt what we cannot
    verify).
    """
    if psycopg is None:
        return True  # cannot verify — insist on the deliberate opt-in
    try:
        with psycopg.connect(raw_url, connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            row = cur.fetchone()
    except Exception as exc:
        _log.warning("restore_target_db_probe_failed error=%s", exc)
        return True
    return bool(row and row[0])


def _check_archived_state_consistency(backup_dir: Path, manifest: dict[str, Any]) -> None:
    """Verify the ARCHIVED state.json matches the manifest's recorded MAC.

    Adoption copies the archive's launcher files over the target's. Before
    any copy, the archived state.json's envelope ``mac`` must equal the
    manifest's recorded ``state_mac`` — a mismatch means the archive's
    components disagree (tampered / partial / mistrustworthy archive) and
    NOTHING is adopted. Refuses when the archive is DESCRIBED as including
    a state.json that is absent from disk.
    """
    identity = manifest.get("launcher_snapshot") or {}
    if not isinstance(identity, dict):
        return
    recorded_mac = identity.get("state_mac")
    archived_state = backup_dir / _LAUNCHER_STATE_FILE
    if identity.get("state_json_included") and not archived_state.exists():
        raise click.ClickException(
            "The archive's manifest claims a launcher state.json was backed up, but the file is NOT in "
            "the archive — refusing to adopt launcher artefacts from an incomplete archive."
        )
    if not archived_state.exists() or recorded_mac is None:
        return
    try:
        archived_mac = _read_state_mac(archived_state)
    except RuntimeError as exc:
        raise click.ClickException(
            f"The archived state.json is unreadable ({exc}) — refusing to adopt launcher artefacts from this archive."
        ) from exc
    if archived_mac != recorded_mac:
        raise click.ClickException(
            "The archive's state.json identity does NOT match the manifest's recorded fingerprint:\n"
            f"  Archived state.json mac: {archived_mac}\n"
            f"  Manifest state_mac:      {recorded_mac}\n"
            "Refusing to adopt launcher artefacts — the archive is inconsistent or tampered with."
        )


def _ensure_restore_posture(admin_url: str, app_url: str) -> None:
    """Re-run the promoted first-boot bootstrap/bootstrap_role ceremony.

    Reuses the promoted path (``modulo.db.bootstrap_role.bootstrap_roles`` —
    ADR 031 Decision 2) so a restored database ends up with exactly the
    role/grant posture first boot would produce: modulo_app NOBYPASSRLS,
    modulo_system/modulo_breakglass BYPASSRLS, grants re-applied, and the
    accounts allow-list + role-posture assertions re-run (fatal inside that
    path). Best-effort at the CLI layer — a failure is a loud warning, not a
    half-restore; the API lifespan re-asserts the same posture.
    """
    import asyncio

    from modulo.db.bootstrap_role import bootstrap_roles

    try:
        asyncio.run(bootstrap_roles(admin_url, app_url))
        click.echo("  Role posture re-verified via the promoted bootstrap path")
    except Exception as exc:
        _log.exception("restore_posture_verification_failed")
        click.echo(
            f"WARNING: role-posture verification FAILED ({exc}) — run bootstrap manually before serving.",
            err=True,
        )


def _collation_versions_same_family(stored: str, detected: str) -> bool:
    """True when two collation versions likely belong to the same family.

    Major-version parity (e.g. glibc 2.28 vs 2.35) is compatible drift —
    warn and proceed. A family change (e.g. musl 1.2.x -> glibc 2.x) is
    INCOMPATIBLE: the ordering semantics changed wholesale. Versions that
    cannot be parsed conservatively refuse (treated as incompatible).
    """
    stored_major = stored.split(".", 1)[0]
    detected_major = detected.split(".", 1)[0]
    if not (stored_major.isdigit() and detected_major.isdigit()):
        return False
    return stored_major == detected_major


def _check_collation_versions_sync(raw_url: str) -> None:
    """Refuse INCOMPATIBLE collation-version drift; warn and proceed on same-family drift.

    The compose -> native path moves a cluster built against an alpine/musl
    Postgres onto a Debian/glibc one: every text index built under the old
    collation version may sort differently under the new one, which corrupts
    index scans silently. Postgres records each collation's creation version
    in ``pg_collation`` and refuses to notice until asked — ask, and:

    * a DIFFERENT version family (musl -> glibc, or an unparsable version)
      is a HARD REFUSAL with the mismatch and the remediation named;
    * a compatible-but-older same-family version (glibc 2.28 -> 2.35) warns
      and proceeds.
    """
    if psycopg is None:
        return
    query = (
        "SELECT collname, collversion, pg_collversion() AS detected_version, collprovider "
        "FROM pg_collation WHERE collversion IS NOT NULL AND collversion <> pg_collversion()"
    )
    try:
        with psycopg.connect(raw_url, connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    except Exception as exc:
        _log.warning("collation_version_check_failed error=%s", exc)
        return
    for row in rows:
        name, stored, detected, provider = row[0], str(row[1]), str(row[2]), row[3]
        if _collation_versions_same_family(stored, detected):
            click.echo(
                f"COLLATION VERSION MISMATCH on collation {name!r} (provider {provider}): the cluster was "
                f"built with collation version {stored} but this Postgres runs {detected}. Text indexes "
                "created under the old version may be in the wrong order under this libc. Run 'reindexdb "
                "--all' after verifying the data. See the postgres 'locale provider / ICU & libc collation "
                "versions' documentation.",
                err=True,
            )
            continue
        raise click.ClickException(
            f"COLLATION VERSION INCOMPATIBLE on collation {name!r} (provider {provider}): the cluster was "
            f"built under collation version {stored} but this Postgres runs {detected} — a DIFFERENT "
            "collation family (the alpine/musl -> glibc compose -> native drift case). Text indexes built "
            "under the source collation can be silently mis-ordered, so the restore is REFUSED. Remediation: "
            "restore onto a target cluster whose libc/collation family matches the source, or rebuild every "
            "text index ('reindexdb --all') after reviewing the restored data. See the postgres 'locale "
            "provider / ICU & libc collation versions' documentation."
        )


# ── Dry-run preview ──────────────────────────────────────────────────────────


def _preview_restore(
    backup_dir: Path,
    manifest: dict[str, Any],
    settings: Any,
    previous_fernet_key: str | None,
) -> None:
    """Preview every restore step without touching the database.

    Integrity checks (checksums, JSON shape) are assumed to have already run.
    Raises the same ClickExceptions the real restore would raise for missing
    inputs, so a dry run surfaces any blocking problem before the operator
    commits to an irreversible restore.
    """
    click.echo("DRY RUN — no changes will be made.")

    db_sql = backup_dir / _DB_SQL_FILE
    if db_sql.exists():
        click.echo(f"  WOULD restore database schema and data via psql from {db_sql.name}")
    else:
        click.echo("  No database.sql found — WOULD skip full DB restore")

    for json_name, label in (
        (_BLOBS_JSON_FILE, "checkpoint blob records"),
        (_CHECKPOINTS_JSON_FILE, "checkpoint records"),
        (_WRITES_JSON_FILE, "checkpoint write records"),
    ):
        json_path = backup_dir / json_name
        if json_path.exists():
            records = json.loads(json_path.read_text(encoding="utf-8"))
            click.echo(f"  WOULD restore {len(records)} {label} from {json_name}")
        else:
            click.echo(f"  No {json_name} found — WOULD skip")

    creds_json = backup_dir / _CREDS_JSON_FILE
    if creds_json.exists():
        creds: dict[str, list[dict[str, Any]]] = json.loads(creds_json.read_text(encoding="utf-8"))
        current_key_hash = _fernet_key_hash(settings.fernet_key)
        backup_key_hash = manifest.get("fernet_key_hash", "")
        if current_key_hash == backup_key_hash:
            click.echo("  FERNET_KEY unchanged — WOULD skip credential re-encryption")
        else:
            if not previous_fernet_key:
                raise click.ClickException(
                    "FERNET_KEY has changed since backup. Provide --previous-fernet-key to re-encrypt credentials."
                )
            total = 0
            for table, rows in creds.items():
                count = sum(1 for row in rows if row.get("credentials_ciphertext"))
                total += count
                if count:
                    click.echo(f"  WOULD re-encrypt {count} {table} credentials")
            if not total:
                click.echo("  FERNET_KEY changed but no credentials carry ciphertext — nothing to re-encrypt")
    else:
        click.echo("  No credentials_references.json found — WOULD skip credential restore")

    click.echo("\nDry run complete — no changes were made.")


# ── CLI ──────────────────────────────────────────────────────────────────────


@click.group()
def cli() -> None:
    """Modulo backup and restore operations."""


@cli.command()
@click.option(
    "--db-url",
    envvar="DATABASE_URL",
    default=None,
    help="Database URL override (default: from settings / DATABASE_URL env)",
)
@click.option(
    "--output-dir",
    "-o",
    default=None,
    type=click.Path(path_type=Path),
    help="Output directory (default: ./modulo-backup-<YYYYMMDD-HHMMSS>)",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Launcher data dir to include (and lock): copies state.json, and the TLS keypair when present. "
        "Passes the secrets file / conf profile only with --include-secrets."
    ),
)
@click.option(
    "--include-secrets",
    is_flag=True,
    default=False,
    help=(
        "Include the 0600 launcher secrets file and the credential-bearing conf profile in the archive. "
        "LOUD WARNING: the archive then carries the generated service passwords."
    ),
)
def backup(db_url: str | None, output_dir: Path | None, data_dir: Path | None, include_secrets: bool) -> None:
    """Create a full backup of the Modulo database.

    Creates a timestamped directory with a pg_dump SQL file, checkpoint_blobs
    JSON export, credentials references, and a versioned backup-info.json
    manifest. With --data-dir, the launcher state rides along (secrets only
    with --include-secrets; Redis/SAQ queue data is ephemeral and excluded
    by design). Taking a data dir requires its exclusive lock — a running
    launcher refuses with its holder PID.
    """
    raw_url = _resolve_url(db_url)
    settings = get_settings()
    from modulo.launcher.env_safety import scrub_os_environment

    scrub_os_environment()

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    # The data-dir lock is taken BEFORE any output directory is created: a
    # refused backup (holder PID in the message) must not leave an empty
    # collision directory behind — nothing exists yet to clean up.
    lock = None
    if data_dir is not None:
        lock = _acquire_data_lock(data_dir, mode="backup")

    user_specified = False
    if output_dir is not None:
        backup_dir = output_dir
        user_specified = True
        backup_dir.mkdir(parents=True, exist_ok=True)
    else:
        user_specified = False
        suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision for the backup folder  # noqa: S311
        backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        while backup_dir.exists():
            suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision  # noqa: S311
            backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        backup_dir.mkdir(parents=True, exist_ok=False)

    file_checksums: dict[str, str] = {}

    try:
        click.echo(f"Backup directory: {backup_dir}")
        _warn_cloud_sync_output(backup_dir)

        launcher_section: dict[str, Any] = {
            "state_json_included": False,
            "state_mac": None,
            "conf_profile": {"included": False, "note": "no data dir given"},
            "secrets_file": {"included": False, "note": "no data dir given"},
            "tls_files": [],
            "tls_note": ("no data dir given — no TLS material in the archive"),
            "ephemeral": {
                "redis_saq_queue_data": (
                    "EXCLUDED by design — Redis/SAQ queue data is ephemeral (ADR 031 Decision 6); "
                    "queued jobs do not survive a backup/restore cycle"
                )
            },
        }
        launcher_checksums: list[str] = []
        if data_dir is not None:
            # ORDERING (locked by test): last_backup_at is recorded BEFORE the
            # launcher snapshot files are copied. _record_last_backup rewrites
            # state.json (payload + HMAC change); snapshotting first would
            # archive the PRE-rewrite file whose mac then no longer matches
            # the data dir's final on-disk state — backup→restore to the SAME
            # instance would always demand --replace-cluster. Recording first
            # makes the manifest's state_mac match the final state.
            _record_last_backup(data_dir, datetime.now(UTC).isoformat())
            launcher_section, launcher_checksums = _launcher_snapshot_files(data_dir, backup_dir, include_secrets)

        click.echo("Running pg_dump...")
        _run_pg_dump(raw_url, backup_dir / _DB_SQL_FILE)
        file_checksums[_DB_SQL_FILE] = _file_checksum(backup_dir / _DB_SQL_FILE)
        click.echo("  database.sql written")

        click.echo("Exporting checkpoint_blobs...")
        blobs = _export_checkpoint_blobs_sync(raw_url)
        _write_json(backup_dir / _BLOBS_JSON_FILE, blobs)
        file_checksums[_BLOBS_JSON_FILE] = _file_checksum(backup_dir / _BLOBS_JSON_FILE)
        click.echo(f"  {len(blobs)} checkpoint blob records exported")

        click.echo("Exporting checkpoints...")
        checkpoints = _export_checkpoints_sync(raw_url)
        _write_json(backup_dir / _CHECKPOINTS_JSON_FILE, checkpoints)
        file_checksums[_CHECKPOINTS_JSON_FILE] = _file_checksum(backup_dir / _CHECKPOINTS_JSON_FILE)
        click.echo(f"  {len(checkpoints)} checkpoint records exported")

        click.echo("Exporting checkpoint_writes...")
        cwrites = _export_checkpoint_writes_sync(raw_url)
        _write_json(backup_dir / _WRITES_JSON_FILE, cwrites)
        file_checksums[_WRITES_JSON_FILE] = _file_checksum(backup_dir / _WRITES_JSON_FILE)
        click.echo(f"  {len(cwrites)} checkpoint write records exported")

        click.echo("Exporting credentials references...")
        creds = _export_credentials_references_sync(raw_url)
        _write_json(backup_dir / _CREDS_JSON_FILE, creds)
        file_checksums[_CREDS_JSON_FILE] = _file_checksum(backup_dir / _CREDS_JSON_FILE)
        total_creds = sum(len(v) for v in creds.values())
        click.echo(f"  {total_creds} credential records referenced")

        ride_along_checksums = {name: _file_checksum(backup_dir / name) for name in launcher_checksums}
        manifest: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "manifest_version": MANIFEST_VERSION,
            "backup_type": "full",
            "db_version": _get_db_version(raw_url),
            "schema_versions": _get_schema_versions(),
            "fernet_key_hash": _fernet_key_hash(settings.fernet_key),
            "file_checksums": {**file_checksums, **ride_along_checksums},
            "launcher_snapshot": launcher_section,
        }
        _write_json(backup_dir / _BACKUP_INFO_FILE, manifest)
        click.echo("  backup-info.json written")

        artifact_names = [
            _BACKUP_INFO_FILE,
            _DB_SQL_FILE,
            _BLOBS_JSON_FILE,
            _CHECKPOINTS_JSON_FILE,
            _WRITES_JSON_FILE,
            _CREDS_JSON_FILE,
            *launcher_checksums,
        ]
        if (backup_dir / _TLS_DIRNAME).is_dir():
            artifact_names.append(_TLS_DIRNAME)
        _lock_artifact_mode(backup_dir, artifact_names, user_specified_dir=user_specified)

        click.echo(f"\nBackup complete: {backup_dir}")
        _print_size(backup_dir)

    except Exception as exc:
        _log.exception("Backup failed")
        if not user_specified and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
            click.echo(f"Cleaned up partial backup directory: {backup_dir}", err=True)
        click.echo(f"Backup failed: {exc}", err=True)
        raise click.ClickException(str(exc)) from exc
    finally:
        if lock is not None:
            lock.release()


@cli.command()
@click.argument("backup_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--db-url",
    envvar="DATABASE_URL",
    default=None,
    help="Database URL override (default: from settings / DATABASE_URL env)",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
@click.option(
    "--previous-fernet-key",
    default=None,
    help="Previous FERNET_KEY if it changed since backup (required for credential re-encryption)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview the restore without making any database changes",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Launcher data dir to lock and refresh: adopts the archive's launcher artefacts "
        "(state.json / secrets.json / conf profile / TLS keypair, where included) and refuses "
        "while that data dir's launcher is running."
    ),
)
@click.option(
    "--replace-cluster",
    is_flag=True,
    default=False,
    help=(
        "Intentionally replace a DIFFERENT populated instance with the archive (per-instance "
        "identity mismatch otherwise refuses)."
    ),
)
@click.option(
    "--no-safety-dump",
    is_flag=True,
    default=False,
    help=(
        "Skip the automatic pre-restore safety dump of the TARGET. LOUD: without it, a mid-import "
        "failure would otherwise abort out of a half-dropped database."
    ),
)
def restore(
    backup_dir: Path,
    db_url: str | None,
    yes: bool,
    previous_fernet_key: str | None,
    dry_run: bool,
    data_dir: Path | None,
    replace_cluster: bool,
    no_safety_dump: bool,
) -> None:
    """Restore a Modulo database from a backup directory.

    Verifies BEFORE it wipes (manifest shape + version, instance identity,
    database-emptiness probe for fresh targets, schema downgrade guard, disk
    pre-flight, file integrity), takes a pre-restore safety dump of the
    target (or --no-safety-dump), restores the database via psql in a single
    transaction, re-inserts checkpoint_blobs from the JSON export,
    re-encrypts credentials if the FERNET_KEY has changed, re-ADOPTS the
    launcher artefacts only after the database restore SUCCEEDS, re-runs the
    promoted bootstrap role-posture check, and checks collation-version drift
    (incompatible families REFUSE the restore; same-family drift warns and
    proceeds). With --dry-run, validates integrity and previews every step
    without touching the database. Against a launcher data dir, takes the
    exclusive lock (a still running launcher refuses with its holder PID).
    """
    raw_url = _resolve_url(db_url)
    settings = get_settings()
    from modulo.launcher.env_safety import scrub_os_environment

    scrub_os_environment()

    # Shape-validate the manifest FIRST — nothing about the archive is echoed
    # before it parses, is a JSON object, and every echoed field is typed.
    manifest = _read_and_validate_manifest(backup_dir)
    click.echo(f"Backup timestamp: {manifest.get('timestamp', 'unknown')}")
    click.echo(f"Backup type: {manifest.get('backup_type', 'unknown')}")
    click.echo(f"DB version at backup: {manifest.get('db_version', 'unknown')}")
    click.echo(f"Schema versions: {', '.join(manifest.get('schema_versions', ['unknown']))}")

    lock = None
    if data_dir is not None:
        # Restore-while-running refuses BEFORE anything is verified as
        # writable, let alone wiped (ADR 031 data-dir lock contract).
        lock = _acquire_data_lock(data_dir, mode="restore")

    try:
        # Verify-before-wipe ordering: identity + disk before any mutation.
        if data_dir is not None:
            _check_instance_identity(data_dir, manifest, replace_cluster)
        _disk_preflight(backup_dir, data_dir)

        if not dry_run and not yes:
            click.confirm("\nThis will OVERWRITE the current database. Continue?", abort=True)

        checksums = manifest.get("file_checksums")
        if checksums:
            click.echo("Verifying backup file integrity...")
            for filename, expected_hash in checksums.items():
                file_path = backup_dir / filename
                if not file_path.exists():
                    raise click.ClickException(f"Backup file {filename} listed in manifest but not found on disk")
                actual_hash = _file_checksum(file_path)
                if actual_hash != expected_hash:
                    raise click.ClickException(
                        f"Checksum mismatch for {filename}: expected {expected_hash}, got {actual_hash}"
                    )
            click.echo("  All file checksums verified")

        for json_name in (
            _BLOBS_JSON_FILE,
            _CHECKPOINTS_JSON_FILE,
            _WRITES_JSON_FILE,
            _CREDS_JSON_FILE,
        ):
            json_path = backup_dir / json_name
            if json_path.exists():
                try:
                    json.loads(json_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise click.ClickException(f"Corrupt JSON file {json_name}: {exc}") from exc

        if dry_run:
            _preview_restore(backup_dir, manifest, settings, previous_fernet_key)
            return

        _check_schema_downgrade(manifest)

        # Fresh-target exemption is about the DATA DIR, not the DATABASE: a
        # populated production database behind a fresh/absent data dir is NOT
        # eligible for the silent exemption — it must be --replace-cluster.
        if data_dir is not None and _db_has_modulo_tables(raw_url) and not (data_dir / _LAUNCHER_STATE_FILE).exists():
            if not replace_cluster:
                raise click.ClickException(
                    "The target database ALREADY CONTAINS application tables, but its data dir is fresh "
                    "(no state.json) — this restore would silently overwrite an existing production "
                    "database. Pass --replace-cluster if you really intend to replace it, or point at a "
                    "clean database."
                )
            click.echo(
                "REPLACING an existing POPULATED database whose data dir was fresh/absent (acknowledged via "
                "--replace-cluster).",
                err=True,
            )

        db_sql = backup_dir / _DB_SQL_FILE
        if db_sql.exists():
            # Pre-restore safety dump of the TARGET (refused/patched via
            # --no-safety-dump) so a mid-import psql failure leaves the
            # previous cluster restorable — psql replays the --clean dump's
            # DROPs under -1 (single transaction), but a half-download or a
            # broken dump must never be the only way back.
            if not no_safety_dump:
                _take_safety_dump(raw_url)
            click.echo("Restoring database schema and data via psql...")
            _run_psql(raw_url, db_sql)
            click.echo("  Database restored from SQL dump")
        else:
            click.echo("  No database.sql found — skipping full DB restore")

        blobs_json = backup_dir / _BLOBS_JSON_FILE
        if blobs_json.exists():
            click.echo("Restoring checkpoint_blobs from JSON export...")
            blobs: list[dict[str, Any]] = json.loads(blobs_json.read_text(encoding="utf-8"))
            restored = _restore_checkpoint_blobs_sync(raw_url, blobs)
            click.echo(f"  {restored} checkpoint blob records restored")
        else:
            click.echo("  No checkpoint_blobs.json found — skipping")

        checkpoints_json = backup_dir / _CHECKPOINTS_JSON_FILE
        if checkpoints_json.exists():
            click.echo("Restoring checkpoints from JSON export...")
            checkpoints_data: list[dict[str, Any]] = json.loads(checkpoints_json.read_text(encoding="utf-8"))
            restored_cp = _restore_checkpoints_sync(raw_url, checkpoints_data)
            click.echo(f"  {restored_cp} checkpoint records restored")
        else:
            click.echo("  No checkpoints.json found — skipping")

        cwrites_json = backup_dir / _WRITES_JSON_FILE
        if cwrites_json.exists():
            click.echo("Restoring checkpoint_writes from JSON export...")
            cwrites_data: list[dict[str, Any]] = json.loads(cwrites_json.read_text(encoding="utf-8"))
            restored_cw = _restore_checkpoint_writes_sync(raw_url, cwrites_data)
            click.echo(f"  {restored_cw} checkpoint write records restored")
        else:
            click.echo("  No checkpoint_writes.json found — skipping")

        creds_json = backup_dir / _CREDS_JSON_FILE
        current_key_hash = _fernet_key_hash(settings.fernet_key)
        backup_key_hash = manifest.get("fernet_key_hash", "")

        if creds_json.exists() and current_key_hash != backup_key_hash:
            if not previous_fernet_key:
                raise click.ClickException(
                    "FERNET_KEY has changed since backup. Provide --previous-fernet-key to re-encrypt credentials."
                )
            click.echo("Re-encrypting credentials with current FERNET_KEY...")
            creds_data: dict[str, list[dict[str, Any]]] = json.loads(creds_json.read_text(encoding="utf-8"))
            counts = _re_encrypt_credentials_sync(raw_url, creds_data, previous_fernet_key, settings.fernet_key)
            for table, cnt in counts.items():
                click.echo(f"  {cnt} {table} re-encrypted")
        elif current_key_hash == backup_key_hash:
            click.echo("FERNET_KEY unchanged — no credential re-encryption needed")

        if db_sql.exists():
            _check_collation_versions_sync(raw_url)

        if data_dir is not None:
            # ORDERING (locked by test): launcher artefacts are adopted only
            # AFTER the database restore SUCCEEDS. Adopting first would leave
            # a psql-failure data dir carrying the ARCHIVE's credentials
            # against the target's OLD database — bricked in both directions
            # (the old launcher no longer has its credentials; restarting the
            # new one hits a database that was never restored).
            _check_archived_state_consistency(backup_dir, manifest)
            adopted = _restore_launcher_files(backup_dir, data_dir)
            if adopted:
                click.echo(f"Adopted launcher artefacts into {data_dir}: {', '.join(adopted)}")

        admin_url = os.environ.get("DATABASE_ADMIN_URL") or raw_url
        app_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        _ensure_restore_posture(admin_url, app_url)

        click.echo("\nRestore complete.")

    except Exception as exc:
        _log.exception("Restore failed")
        click.echo(f"Restore failed: {exc}", err=True)
        raise click.ClickException(str(exc)) from exc
    finally:
        if lock is not None:
            lock.release()


# ---------------------------------------------------------------------------
# modulo apply (FAR-681)
# ---------------------------------------------------------------------------
# Registered once at import time so the ``modulo apply`` subcommand is wired
# into the CLI group. Wrapped in an ``_init_once_*`` helper because the
# module-side-effects architecture test forbids bare module-level calls.
def _init_once_register_apply() -> None:
    register_apply(cli)


_init_once_register_apply()
