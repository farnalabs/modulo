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
  path re-runs, and the Postgres collation-version table is checked with a
  hard warning on drift (the compose musl -> native glibc path).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import subprocess  # nosec B404 -- subprocess is used only to shell out to pg_dump/psql with a hardcoded argv list (never shell=True, never user-supplied commands); see _run_pg_dump/_run_psql
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
# archive. Detected by path marker and warned about loudly.
_CLOUD_SYNC_MARKERS = ("dropbox", "onedrive", "icloud")

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
    with path.open("w", encoding="utf-8") as f:
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


def _run_pg_dump(raw_url: str, output: Path, timeout: int = 300) -> None:
    _check_tool("pg_dump")
    cmd = [
        "pg_dump",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--format=plain",
        raw_url,
    ]
    try:
        with output.open("wb") as f:
            result = subprocess.run(  # nosec B603 -- cmd is a hardcoded argv list, never shell=True, never user input  # noqa: S603
                cmd, stdout=f, stderr=subprocess.PIPE, check=False, timeout=timeout
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pg_dump timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()}")


def _run_psql(raw_url: str, input_path: Path, timeout: int = 600) -> None:
    _check_tool("psql")
    cmd = ["psql", "-q", "-v", "ON_ERROR_STOP=1", raw_url]
    try:
        with input_path.open("rb") as f:
            result = subprocess.run(  # nosec B603 -- cmd is a hardcoded argv list with trusted input, never shell=True  # noqa: S603
                cmd, stdin=f, capture_output=True, check=False, timeout=timeout
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"psql restore timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"psql restore failed: {result.stderr.decode(errors='replace').strip()}")


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
    lowered = {part.lower() for part in backup_dir.parts}
    hit = sorted(marker for marker in _CLOUD_SYNC_MARKERS if any(marker in part for part in lowered))
    if hit:
        click.echo(
            f"WARNING: backup target {backup_dir} is inside a cloud-synced folder ({', '.join(hit)}). "
            "Sync churn can corrupt the dump mid-write and the archive leaves the machine automatically. "
            "Back up to a local directory and copy the finished archive explicitly instead.",
            err=True,
        )


def _lock_artifact_mode(backup_dir: Path) -> None:
    """Tighten the archive artifacts to owner-only (0600/0700, POSIX).

    The default archive is credential-free, but it contains connector and
    checkpoint data — keep it out of group/other reach. TODO(P3): Windows
    ACL hardening (chmod is a silent no-op there).
    """
    if os.name != "posix":
        return
    try:
        backup_dir.chmod(0o700)
        for path in backup_dir.rglob("*"):
            path.chmod(0o600)
    except OSError as exc:
        _log.warning("backup_artifact_mode_failed path=%s error=%s", backup_dir, exc)


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


def _launcher_snapshot_files(data_dir: Path, backup_dir: Path, include_secrets: bool) -> dict[str, Any]:
    """Copy the launcher data-dir artefacts into the archive; describe them.

    Included unconditionally (when present): ``state.json`` — credential-free
    by contract. Included ONLY with ``--include-secrets``: the 0600 secrets
    file and the credential-bearing conf profile (both carry the generated
    service passwords; a default archive must never contain them). TLS
    keypair files ride along when the data dir has a ``tls/`` directory.
    Returns the manifest section describing what was (or was not) included.
    """
    section: dict[str, Any] = {
        "state_json_included": False,
        "state_mac": None,
        "conf_profile": {"included": False, "note": "excluded by default (carries generated credentials)"},
        "secrets_file": {"included": False, "note": "excluded by default (generated credentials)"},
        "tls_files": [],
        "ephemeral": {
            "redis_saq_queue_data": (
                "EXCLUDED by design — Redis/SAQ queue data is ephemeral (ADR 031 Decision 6); "
                "queued jobs do not survive a backup/restore cycle"
            )
        },
    }
    state_path = data_dir / _LAUNCHER_STATE_FILE
    if state_path.exists():
        shutil.copy2(state_path, backup_dir / _LAUNCHER_STATE_FILE)
        section["state_json_included"] = True
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
        if conf_path.exists():
            shutil.copy2(conf_path, backup_dir / _LAUNCHER_CONF_FILE)
            section["conf_profile"]["included"] = True
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
    return section


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
    except Exception as exc:  # collapse: the timestamp bookkeeping must never break the backup itself
        _log.warning("backup_timestamp_not_recorded data_dir=%s error=%s", data_dir, exc)


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
    * fresh / uninitialized target (no state.json) — EXEMPT: disaster
      recovery and machine migration restore onto empty data dirs.
    * manifest without a fingerprint (legacy archive) — cannot compare;
      warn and proceed.
    * mismatch — REFUSED unless ``--replace-cluster`` (loud confirmation
      printing both identifiers; ``--yes`` cannot suppress that
      confirmation).
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
    click.confirm("Replace the target instance with the archived one?", abort=True)


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


def _check_collation_versions_sync(raw_url: str) -> None:
    """Hard warning when pg_collation's stored versions drifted (musl->glibc).

    The compose -> native path moves a cluster built against an alpine/musl
    Postgres onto a Debian/glibc one: every text index built under the old
    collation version may sort differently under the new one, which corrupts
    index scans silently. Postgres records each collation's creation version
    in ``pg_collation`` and refuses to notice until asked — ask, and refuse
    to stay quiet about drift.
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
        click.echo(
            f"COLLATION VERSION MISMATCH on collation {row[0]!r} (provider {row[3]}): the cluster was "
            f"built with collation version {row[1]} but this Postgres runs {row[2]}. This is exactly "
            "the alpine/musl -> glibc (compose -> native) drift case — text indexes created under the "
            "old version may be in the wrong order under this libc. Run 'reindexdb --all' after "
            "verifying the data, or restore onto a cluster matching the original collation version. "
            "See the postgres 'locale provider / ICU & libc collation versions' documentation.",
            err=True,
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

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if output_dir is not None:
        backup_dir = output_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        user_specified = True
    else:
        user_specified = False
        suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision for the backup folder  # noqa: S311
        backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        while backup_dir.exists():
            suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision  # noqa: S311
            backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        backup_dir.mkdir(parents=True, exist_ok=False)

    lock = None
    if data_dir is not None:
        lock = _acquire_data_lock(data_dir, mode="backup")

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
            "ephemeral": {
                "redis_saq_queue_data": (
                    "EXCLUDED by design — Redis/SAQ queue data is ephemeral (ADR 031 Decision 6); "
                    "queued jobs do not survive a backup/restore cycle"
                )
            },
        }
        if data_dir is not None:
            launcher_section = _launcher_snapshot_files(data_dir, backup_dir, include_secrets)

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

        manifest: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "manifest_version": MANIFEST_VERSION,
            "backup_type": "full",
            "db_version": _get_db_version(raw_url),
            "schema_versions": _get_schema_versions(),
            "fernet_key_hash": _fernet_key_hash(settings.fernet_key),
            "file_checksums": file_checksums,
            "launcher_snapshot": launcher_section,
        }
        _write_json(backup_dir / _BACKUP_INFO_FILE, manifest)
        click.echo("  backup-info.json written")

        _lock_artifact_mode(backup_dir)
        if data_dir is not None:
            _record_last_backup(data_dir, manifest["timestamp"])

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
def restore(
    backup_dir: Path,
    db_url: str | None,
    yes: bool,
    previous_fernet_key: str | None,
    dry_run: bool,
    data_dir: Path | None,
    replace_cluster: bool,
) -> None:
    """Restore a Modulo database from a backup directory.

    Verifies BEFORE it wipes (manifest version, instance identity, disk
    pre-flight, file integrity), restores the database via psql, re-inserts
    checkpoint_blobs from the JSON export, re-encrypts credentials if the
    FERNET_KEY has changed, re-runs the promoted bootstrap role-posture
    check, and hard-warns on collation-version drift. With --dry-run,
    validates integrity and previews every step without touching the
    database. Against a launcher data dir, takes the exclusive lock (a still
    running launcher refuses with its holder PID) and a populated-instance
    identity mismatch refuses unless --replace-cluster is confirmed.
    """
    raw_url = _resolve_url(db_url)
    settings = get_settings()

    manifest_path = backup_dir / _BACKUP_INFO_FILE
    if not manifest_path.exists():
        raise click.ClickException(f"{_BACKUP_INFO_FILE} not found in {backup_dir}")

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    click.echo(f"Backup timestamp: {manifest.get('timestamp', 'unknown')}")
    click.echo(f"Backup type: {manifest.get('backup_type', 'unknown')}")
    click.echo(f"DB version at backup: {manifest.get('db_version', 'unknown')}")
    click.echo(f"Schema versions: {', '.join(manifest.get('schema_versions', ['unknown']))}")
    _check_manifest_version(manifest)

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

        if data_dir is not None:
            adopted = _restore_launcher_files(backup_dir, data_dir)
            if adopted:
                click.echo(f"Adopted launcher artefacts into {data_dir}: {', '.join(adopted)}")

        db_sql = backup_dir / _DB_SQL_FILE
        if db_sql.exists():
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

        _check_collation_versions_sync(raw_url)

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
