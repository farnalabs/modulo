"""modulo backup/restore: CLI for self-hosted backup and disaster recovery.

Usage:
  modulo backup [--db-url <url>] [--output-dir <path>]
  modulo restore <backup-dir> [--db-url <url>] [--yes] [--previous-fernet-key <key>]
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from cryptography.fernet import Fernet, InvalidToken

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

from modulo.settings import get_settings

_log = logging.getLogger(__name__)


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


# ── JSON serialisation helpers ────────────────────────────────────────────────


def _serialise_for_json(obj: Any) -> Any:
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


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
    import shutil
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
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=timeout)  # noqa: S603 — cmd is a hardcoded list, not user input
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pg_dump timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode(errors='replace').strip()}")


def _run_psql(raw_url: str, input_path: Path, timeout: int = 600) -> None:
    _check_tool("psql")
    cmd = ["psql", "-q", "-v", "ON_ERROR_STOP=1", raw_url]
    try:
        with input_path.open("rb") as f:
            result = subprocess.run(cmd, stdin=f, capture_output=True, timeout=timeout)  # noqa: S603 — cmd is a hardcoded list with trusted input
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"psql restore timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"psql restore failed: {result.stderr.decode(errors='replace').strip()}")


# ── Sync data export (via psycopg) ────────────────────────────────────────────


def _export_checkpoint_blobs_sync(raw_url: str) -> list[dict[str, Any]]:
    if psycopg is None:
        raise RuntimeError("psycopg library is not available")
    rows: list[dict[str, Any]] = []
    with psycopg.connect(raw_url, row_factory=dict_row, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM checkpoint_blobs ORDER BY organisation_id, thread_id, checkpoint_ns, channel, version"
        )
        for row in cur:
            org_id = row.get("organisation_id")
            row["organisation_id"] = str(org_id) if org_id is not None else None
            if isinstance(row.get("blob"), (bytes, memoryview)):
                row["blob"] = bytes(row["blob"]).hex()
            rows.append(row)
    return rows


_CREDENTIALS_TABLES: list[str] = ["connector_instances", "model_backends"]


def _export_credentials_references_sync(raw_url: str) -> dict[str, list[dict[str, Any]]]:
    if psycopg is None:
        raise RuntimeError("psycopg library is not available")
    result: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(raw_url, row_factory=dict_row, connect_timeout=10) as conn:
        for table in _CREDENTIALS_TABLES:
            rows: list[dict[str, Any]] = []
            with conn.cursor() as cur:
                assert table in _CREDENTIALS_TABLES, f"Unexpected credentials table: {table}"
                sql = f"SELECT id, organisation_id, name, credentials_ciphertext FROM {table} ORDER BY id"  # noqa: S608 — guarded by whitelist assertion
                cur.execute(sql)
                for row in cur:
                    org_id = row.get("organisation_id")
                    row["id"] = str(row["id"])
                    row["organisation_id"] = str(org_id) if org_id is not None else None
                    if isinstance(row.get("credentials_ciphertext"), (bytes, memoryview)):
                        ct = bytes(row["credentials_ciphertext"])
                        row["credentials_ciphertext"] = ct.hex()  # nosemgrep: credential-not-in-state
                    rows.append(row)
            result[table] = rows
    return result


def _restore_checkpoint_blobs_sync(raw_url: str, blobs: list[dict[str, Any]]) -> int:
    if psycopg is None:
        raise RuntimeError("psycopg library is not available")
    with psycopg.connect(raw_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoint_blobs CASCADE")
            for row in blobs:
                blob: bytes | None = None
                raw_blob = row.get("blob")
                if raw_blob is not None:
                    blob = bytes.fromhex(raw_blob) if raw_blob else b""
                org_id_raw = row.get("organisation_id")
                if org_id_raw:
                    try:
                        org_uuid = uuid.UUID(org_id_raw)
                    except (ValueError, TypeError) as exc:
                        raise RuntimeError(f"Invalid organisation_id in checkpoint_blobs: {org_id_raw!r}") from exc
                else:
                    org_uuid = None
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


def _re_encrypt_credentials_sync(
    raw_url: str,
    creds: dict[str, list[dict[str, Any]]],
    old_fernet_key: str,
    new_fernet_key: str,
) -> dict[str, int]:
    if psycopg is None:
        raise RuntimeError("psycopg library is not available")
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
                            f"Failed to decrypt {table} row {row.get('id', '?')}: "
                            "--previous-fernet-key may be wrong"
                        ) from exc
                    new_ct = new_fernet.encrypt(plaintext)
                    try:
                        row_id = uuid.UUID(row["id"])
                    except (ValueError, TypeError):
                        _log.warning("Invalid UUID in credentials row: %s", row.get("id", "?"))
                        continue
                    assert table in _CREDENTIALS_TABLES, f"Unexpected credentials table: {table}"
                    cur.execute(
                        f"UPDATE {table} SET credentials_ciphertext = %s WHERE id = %s",  # noqa: S608 — guarded by whitelist assertion
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
    except (OSError, PermissionError) as exc:
        _log.warning("Could not compute backup size: %s", exc)
        return
    click.echo(f"Total size: {_human_size(total)}")


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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
def backup(db_url: str | None, output_dir: Path | None) -> None:
    """Create a full backup of the Modulo database.

    Creates a timestamped directory with a pg_dump SQL file, checkpoint_blobs
    JSON export, credentials references, and a backup-info.json manifest.
    """
    raw_url = _resolve_url(db_url)
    settings = get_settings()

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if output_dir is not None:
        backup_dir = output_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
    else:
        import random
        suffix = random.randint(1000, 9999)  # noqa: S311 — not crypto, just avoiding directory collision
        backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        while backup_dir.exists():
            suffix = random.randint(1000, 9999)  # noqa: S311
            backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        backup_dir.mkdir(parents=True, exist_ok=False)

    try:
        click.echo(f"Backup directory: {backup_dir}")

        click.echo("Running pg_dump...")
        _run_pg_dump(raw_url, backup_dir / "database.sql")
        click.echo("  database.sql written")

        click.echo("Exporting checkpoint_blobs...")
        blobs = _export_checkpoint_blobs_sync(raw_url)
        _write_json(backup_dir / "checkpoint_blobs.json", blobs)
        click.echo(f"  {len(blobs)} checkpoint blob records exported")

        click.echo("Exporting credentials references...")
        creds = _export_credentials_references_sync(raw_url)
        _write_json(backup_dir / "credentials_references.json", creds)
        total_creds = sum(len(v) for v in creds.values())
        click.echo(f"  {total_creds} credential records referenced")

        manifest: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "backup_type": "full",
            "db_version": _get_db_version(raw_url),
            "schema_versions": _get_schema_versions(),
            "fernet_key_hash": _fernet_key_hash(settings.fernet_key),
        }
        _write_json(backup_dir / "backup-info.json", manifest)
        click.echo("  backup-info.json written")

        click.echo(f"\nBackup complete: {backup_dir}")
        _print_size(backup_dir)

    except Exception as exc:
        _log.exception("Backup failed")
        click.echo(f"Backup failed: {exc}", err=True)
        raise click.ClickException(str(exc)) from exc


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
def restore(backup_dir: Path, db_url: str | None, yes: bool, previous_fernet_key: str | None) -> None:
    """Restore a Modulo database from a backup directory.

    Validates the backup manifest, restores the database via psql, re-inserts
    checkpoint_blobs from the JSON export, and re-encrypts credentials if the
    FERNET_KEY has changed.
    """
    raw_url = _resolve_url(db_url)
    settings = get_settings()

    manifest_path = backup_dir / "backup-info.json"
    if not manifest_path.exists():
        raise click.ClickException(f"backup-info.json not found in {backup_dir}")

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    click.echo(f"Backup timestamp: {manifest.get('timestamp', 'unknown')}")
    click.echo(f"Backup type: {manifest.get('backup_type', 'unknown')}")
    click.echo(f"DB version at backup: {manifest.get('db_version', 'unknown')}")
    click.echo(f"Schema versions: {', '.join(manifest.get('schema_versions', ['unknown']))}")

    if not yes:
        click.confirm("\nThis will OVERWRITE the current database. Continue?", abort=True)

    try:
        db_sql = backup_dir / "database.sql"
        if db_sql.exists():
            click.echo("Restoring database schema and data via psql...")
            _run_psql(raw_url, db_sql)
            click.echo("  Database restored from SQL dump")
        else:
            click.echo("  No database.sql found — skipping full DB restore")

        blobs_json = backup_dir / "checkpoint_blobs.json"
        if blobs_json.exists():
            click.echo("Restoring checkpoint_blobs from JSON export...")
            blobs: list[dict[str, Any]] = json.loads(blobs_json.read_text(encoding="utf-8"))
            restored = _restore_checkpoint_blobs_sync(raw_url, blobs)
            click.echo(f"  {restored} checkpoint blob records restored")
        else:
            click.echo("  No checkpoint_blobs.json found — skipping")

        creds_json = backup_dir / "credentials_references.json"
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

        click.echo("\nRestore complete.")

    except Exception as exc:
        _log.exception("Restore failed")
        click.echo(f"Restore failed: {exc}", err=True)
        raise click.ClickException(str(exc)) from exc
