"""Integration test for migration 0120_org_fk_hardening (FAR-294).

Runs the real Alembic ``upgrade`` of revision 0120 against a live Postgres
(testcontainers) and verifies the org-FK hardening behaves correctly on a
*drifted* schema where some ``organisation_id`` columns lack a foreign key:

  * a clean table (no orphaned references) gets its FK re-added as
    ``ON DELETE CASCADE``, matching the rest of the schema and the org
    hard-delete contract;
  * a table that still holds orphaned rows is left untouched — the rows are
    surfaced for triage, not silently deleted; and
  * hard-deleting an organisation still cascades into a newly-constrained table
    (the ``confirm_org_deletion`` -> ``session.delete(org)`` flow relies on
    this FK cascade).

We use two synthetic tables so the test never mutates the real migrated schema
and never disturbs other integration tests.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

# Synthetic drifted tables: an organisation_id column with NO foreign key yet —
# the exact shape of a prod table the migration is meant to harden.
CLEAN_TABLE = "t_orgfk_clean"
ORPHAN_TABLE = "t_orgfk_orphan"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    # Skip fileConfig so we don't stomp module loggers (mirrors conftest).
    config.config_file_name = None
    return config


async def _count_fk(engine, table: str) -> list[str]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE contype = 'f' "
                        "AND conrelid = (SELECT oid FROM pg_class WHERE relname = :t)"
                    ),
                    {"t": table},
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def _fk_delete_rule(engine, table: str, constraint: str) -> str:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conname = :c "
                    "AND conrelid = (SELECT oid FROM pg_class WHERE relname = :t)"
                ),
                {"c": constraint, "t": table},
            )
        ).scalar()


async def test_0120_org_fk_hardening_on_drifted_schema(migrated_db_url, monkeypatch) -> None:
    db_url = migrated_db_url
    # Point alembic at the (already migrated) testcontainer DB so the real
    # env.py upgrade path runs 0120 against live Postgres.
    monkeypatch.setenv("DATABASE_URL", db_url)
    config = _alembic_config(db_url)

    engine = create_async_engine(db_url, poolclass=NullPool)

    # Synthetic drifted tables: an organisation_id column with NO foreign key yet.
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS t_orgfk_clean"))
        await conn.execute(text("DROP TABLE IF EXISTS t_orgfk_orphan"))
        await conn.execute(text("CREATE TABLE t_orgfk_clean (id UUID PRIMARY KEY, organisation_id UUID NOT NULL)"))
        await conn.execute(text("CREATE TABLE t_orgfk_orphan (id UUID PRIMARY KEY, organisation_id UUID NOT NULL)"))

    org_valid = uuid.uuid4()
    org_orphan = uuid.uuid4()  # intentionally never inserted into organisations
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
            {"id": str(org_valid), "n": "valid-org", "s": "valid-org"},
        )
        # Clean table: every row references a real organisation.
        await conn.execute(
            text("INSERT INTO t_orgfk_clean (id, organisation_id) VALUES (:id, :o)"),
            {"id": str(uuid.uuid4()), "o": str(org_valid)},
        )
        await conn.execute(
            text("INSERT INTO t_orgfk_clean (id, organisation_id) VALUES (:id, :o)"),
            {"id": str(uuid.uuid4()), "o": str(org_valid)},
        )
        # Orphan table: every row references a non-existent organisation.
        await conn.execute(
            text("INSERT INTO t_orgfk_orphan (id, organisation_id) VALUES (:id, :o)"),
            {"id": str(uuid.uuid4()), "o": str(org_orphan)},
        )
        await conn.execute(
            text("INSERT INTO t_orgfk_orphan (id, organisation_id) VALUES (:id, :o)"),
            {"id": str(uuid.uuid4()), "o": str(org_orphan)},
        )

    try:
        # Reset alembic to the revision just before 0120, then re-run 0120 against
        # the drifted schema (the synthetic tables now lack their org FK).
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = '0119_analytics_batch_id'"))
        command.upgrade(config, "0120_org_fk_hardening")

        # --- Migration outcome assertions ---
        clean_fks = await _count_fk(engine, CLEAN_TABLE)
        orphan_fks = await _count_fk(engine, ORPHAN_TABLE)

        # Clean table gets the FK re-added...
        assert f"fk_{CLEAN_TABLE}_organisation_id" in clean_fks, clean_fks
        # ...as ON DELETE CASCADE (matches the schema convention + org-deletion contract).
        delete_rule = await _fk_delete_rule(engine, CLEAN_TABLE, f"fk_{CLEAN_TABLE}_organisation_id")
        assert delete_rule == "c", f"expected ON DELETE CASCADE ('c'), got {delete_rule!r}"

        # Orphaned table is left untouched (surfaced for triage, not auto-deleted).
        assert f"fk_{ORPHAN_TABLE}_organisation_id" not in orphan_fks, orphan_fks

        # --- Org hard-deletion must still cascade into the newly-constrained table ---
        org_delete = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)"),
                {"id": str(org_delete), "n": "del-org", "s": "del-org"},
            )
            await conn.execute(
                text("INSERT INTO t_orgfk_clean (id, organisation_id) VALUES (:id, :o)"),
                {"id": str(uuid.uuid4()), "o": str(org_delete)},
            )

        # Mirrors confirm_org_deletion's hard-delete step (session.delete(org)
        # relying on Postgres FK cascade). We delete directly to avoid the
        # global terminal-run purge that confirm_org_deletion triggers.
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM organisations WHERE id = :o"),
                {"o": str(org_delete)},
            )

        async with engine.connect() as conn:
            remaining = (
                await conn.execute(
                    text("SELECT count(*) FROM t_orgfk_clean WHERE organisation_id = :o"),
                    {"o": str(org_delete)},
                )
            ).scalar()
        assert remaining == 0, "org hard-delete should cascade-remove rows in the newly-constrained table"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_orgfk_clean"))
            await conn.execute(text("DROP TABLE IF EXISTS t_orgfk_orphan"))
            await conn.execute(text("DELETE FROM organisations WHERE slug IN ('valid-org', 'del-org')"))
            # Best-effort restore of the migration head on the shared test DB.
            await conn.execute(text("UPDATE alembic_version SET version_num = '0120_org_fk_hardening'"))
        await engine.dispose()
