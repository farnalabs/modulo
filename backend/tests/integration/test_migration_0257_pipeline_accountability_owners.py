"""Migration 0257: pipeline accountability-owner columns (FAR-1161 data layer).

Runs against the migrated testcontainer (test_initial_migration harness):
asserts the two nullable UUID columns exist on ``pipelines``, both are
nullable, both carry a foreign key to ``accounts`` with ON DELETE SET NULL,
and both indexes from the migration exist.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def _column_info(db_engine: AsyncEngine, table: str) -> dict[str, dict[str, object]]:
    async with db_engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: {
                col["name"]: {"type": col["type"], "nullable": col["nullable"]}
                for col in inspect(sync_connection).get_columns(table)
            },
        )


async def test_pipelines_gains_accountability_owner_columns(db_engine: AsyncEngine) -> None:
    cols = await _column_info(db_engine, "pipelines")
    for expected in ("business_owner_id", "reliability_owner_id"):
        assert expected in cols, f"pipelines missing column {expected}"
        assert cols[expected]["nullable"] is True, f"{expected} must be nullable"


async def test_owner_columns_have_set_null_foreign_keys_to_accounts(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT a.attname, con.confdeltype "
                    "FROM pg_constraint con "
                    "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey) "
                    "WHERE con.conrelid = 'public.pipelines'::regclass "
                    "AND con.contype = 'f' "
                    "AND con.confrelid = 'public.accounts'::regclass"
                )
            )
        ).all()
    fk_by_column = {row[0]: row[1] for row in rows}
    for expected in ("business_owner_id", "reliability_owner_id"):
        assert expected in fk_by_column, f"missing FK pipelines.{expected} -> accounts.id"
        # confdeltype 'n' = SET NULL, 'a' = NO ACTION, 'c' = CASCADE
        # (returned as bytes by psycopg/asyncpg — decode before comparing).
        confdeltype = fk_by_column[expected]
        if isinstance(confdeltype, bytes):
            confdeltype = confdeltype.decode()
        assert confdeltype == "n", f"pipelines.{expected} FK ON DELETE must be SET NULL, got {confdeltype!r}"


async def test_owner_column_indexes_exist(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda sync_connection: {
                row[0]
                for row in sync_connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'pipelines'")
                )
            }
        )
    assert "ix_pipelines_business_owner_id" in indexes
    assert "ix_pipelines_reliability_owner_id" in indexes
