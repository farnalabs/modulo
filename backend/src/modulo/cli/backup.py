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
from cryptography.fernet import Fernet

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


def _get_schema_versions() -> list[str]:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = Path(__file__).resolve().parent.parent.parent.parent  # src -> backend
        alembic_ini = backend_dir / "alembic.ini"
        if not alembic_ini.exists():
            return ["unknown"]
        cfg = Config(str(alembic_ini))
        script = ScriptDirectory.from_config(cfg)
        return sorted(script.get_heads())
    except Exception:
        return ["unknown"]


def _get_db_version(raw_url: str) -> str:
    try:
        import psycopg

        with psycopg.connect(raw_url) as conn:
            row = conn.execute("SELECT version()").fetchone()
            return row[0] if row else "unknown"
    except Exception:
        return "unknown"


# ── pg_dump / psql helpers ────────────────────────────────────────────────────


def _run_pg_dump(raw_url: str, output: Path, timeout: int = 300) -> None:
    cmd = [
        "pg_dump",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--format=plain",
        raw_url,
    ]
    with output.open("wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=timeout)  # noqa: S603 — cmd is a hardcoded list, not user input
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode().strip()}")


def _run_psql(raw_url: str, input_path: Path, timeout: int = 600) -> None:
    cmd = ["psql", "-q", "-v", "ON_ERROR_STOP=1", raw_url]
    with input_path.open("rb") as f:
        result = subprocess.run(cmd, stdin=f, capture_output=True, timeout=timeout)  # noqa: S603 — cmd is a hardcoded list with trusted input
    if result.returncode != 0:
        raise RuntimeError(f"psql restore failed: {result.stderr.decode().strip()}")


# ── Sync data export (via psycopg) ────────────────────────────────────────────


def _export_checkpoint_blobs_sync(raw_url: str) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    rows: list[dict[str, Any]] = []
    with psycopg.connect(raw_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM checkpoint_blobs "
                "ORDER BY organisation_id, thread_id, checkpoint_ns, channel, version"
            )
            for row in cur:
                row["organisation_id"] = str(row["organisation_id"])
                if isinstance(row.get("blob"), (bytes, memoryview)):
                    row["blob"] = bytes(row["blob"]).hex()
                rows.append(row)
    return rows


def _export_credentials_references_sync(raw_url: str) -> dict[str, list[dict[str, Any]]]:
    import psycopg
    from psycopg.rows import dict_row

    result: dict[str, list[dict[str, Any]]] = {}
    tables = ["connector_instances", "model_backends"]
    with psycopg.connect(raw_url, row_factory=dict_row) as conn:
        for table in tables:
            rows: list[dict[str, Any]] = []
            with conn.cursor() as cur:
                _sql = f"SELECT id, organisation_id, name, credentials_ciphertext FROM {table} ORDER BY id"  # noqa: S608  # nosec B608
                cur.execute(_sql)
                for row in cur:
                    row["id"] = str(row["id"])
                    row["organisation_id"] = str(row["organisation_id"])
                    if isinstance(row.get("credentials_ciphertext"), (bytes, memoryview)):
                        row["credentials_ciphertext"] = bytes(row["credentials_ciphertext"]).hex()
                    rows.append(row)
            result[table] = rows
    return result


def _restore_checkpoint_blobs_sync(raw_url: str, blobs: list[dict[str, Any]]) -> int:
    import psycopg

    with psycopg.connect(raw_url) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoint_blobs")
            for row in blobs:
                blob: bytes | None = None
                if row.get("blob"):
                    blob = bytes.fromhex(row["blob"])
                cur.execute(
                    "INSERT INTO checkpoint_blobs "
                    "(organisation_id, thread_id, checkpoint_ns, channel, version, type, blob) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.UUID(row["organisation_id"]),
                        row["thread_id"],
                        row["checkpoint_ns"],
                        row["channel"],
                        row["version"],
                        row["type"],
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
    import psycopg

    old_fernet = Fernet(old_fernet_key.encode())
    new_fernet = Fernet(new_fernet_key.encode())
    counts: dict[str, int] = {}

    with psycopg.connect(raw_url) as conn:
        with conn.cursor() as cur:
            for table, rows in creds.items():
                rekeyed = 0
                for row in rows:
                    hex_ct = row.get("credentials_ciphertext", "")
                    if not hex_ct:
                        continue
                    old_ct = bytes.fromhex(hex_ct)
                    plaintext = old_fernet.decrypt(old_ct)
                    new_ct = new_fernet.encrypt(plaintext)
                    cur.execute(
                        f"UPDATE {table} SET credentials_ciphertext = %s WHERE id = %s",  # noqa: S608  # nosec B608
                        (new_ct, uuid.UUID(row["id"])),
                    )
                    rekeyed += 1
                counts[table] = rekeyed
        conn.commit()
    return counts


# ── Size helper ──────────────────────────────────────────────────────────────


def _print_size(backup_dir: Path) -> None:
    total = sum(f.stat().st_size for f in backup_dir.rglob("*") if f.is_file())
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
    backup_dir = output_dir or Path(f"./modulo-backup-{ts}")
    backup_dir.mkdir(parents=True, exist_ok=True)

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
                    "FERNET_KEY has changed since backup. "
                    "Provide --previous-fernet-key to re-encrypt credentials."
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
        click.echo(f"Restore failed: {exc}", err=True)
        raise click.ClickException(str(exc)) from exc
