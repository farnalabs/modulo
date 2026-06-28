"""Migrate unencrypted checkpoint blobs to encrypted format.

Usage:
    python scripts/migrate-checkpoint-blobs.py [--dry-run]

Connects to the database and encrypts all plaintext blobs in
checkpoint_blobs and checkpoint_writes tables using the configured
FERNET_KEY.

Fernet-encrypted blobs start with ``gAAAAA`` (base64-encoded ``gAAAA``),
so we skip any blob that already starts with that prefix.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import sqlalchemy
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from modulo.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger("migrate-checkpoint-blobs")


def _is_encrypted(blob: bytes | None) -> bool:
    """Return True if *blob* already looks Fernet-encrypted."""
    if blob is None:
        return True
    try:
        return blob[:6] == b"gAAAAA"
    except Exception:
        return False


async def _migrate_table(
    conn_string: str,
    fernet: Fernet,
    table: str,
    pk_columns: list[str],
    blob_column: str,
    dry_run: bool,
) -> int:
    """Encrypt all unencrypted blobs in *table*.

    Returns the number of rows that would be / were updated.
    """
    engine = create_async_engine(conn_string)
    try:
        async with engine.connect() as conn:
            tbl = sqlalchemy.table(table)
            cols = [tbl.c[c] for c in pk_columns]
            result = await conn.execute(select(*cols, tbl.c[blob_column]))
            rows = result.fetchall()

        encrypted_count = 0
        for row in rows:
            row_dict = dict(row._mapping)
            blob = row_dict[blob_column]
            if _is_encrypted(blob):
                continue

            raw: bytes = bytes(blob)
            encrypted: bytes = fernet.encrypt(raw)
            encrypted_count += 1

            if not dry_run:
                pk_conditions = [tbl.c[col] == row_dict[col] for col in pk_columns]
                stmt = update(tbl).where(*pk_conditions).values(**{blob_column: encrypted})
                async with engine.connect() as conn:
                    await conn.execute(stmt)
                    await conn.commit()

        return encrypted_count
    finally:
        await engine.dispose()


async def migrate(dry_run: bool) -> None:
    settings = get_settings()

    if not settings.fernet_key:
        _log.error("FERNET_KEY not configured — cannot encrypt blobs")
        sys.exit(1)

    fernet = Fernet(settings.fernet_key.encode())
    conn_string = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    blobs_count = await _migrate_table(
        conn_string,
        fernet,
        table="checkpoint_blobs",
        pk_columns=[
            "organisation_id",
            "thread_id",
            "checkpoint_ns",
            "channel",
            "version",
        ],
        blob_column="blob",
        dry_run=dry_run,
    )

    writes_count = await _migrate_table(
        conn_string,
        fernet,
        table="checkpoint_writes",
        pk_columns=[
            "organisation_id",
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
        ],
        blob_column="blob",
        dry_run=dry_run,
    )

    action = "Would encrypt" if dry_run else "Encrypted"
    _log.info("%s %d checkpoint_blobs rows", action, blobs_count)
    _log.info("%s %d checkpoint_writes rows", action, writes_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Encrypt unencrypted checkpoint blobs at rest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rows to encrypt without modifying data",
    )
    args = parser.parse_args()

    asyncio.run(migrate(args.dry_run))
