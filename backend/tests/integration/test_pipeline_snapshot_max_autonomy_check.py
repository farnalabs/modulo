"""FAR-1223: ``pipeline_snapshots.max_autonomy_level`` is CHECK-guarded.

Migration 0256 added the ceiling column to BOTH ``pipelines`` and
``pipeline_snapshots`` but only guarded the ``pipelines`` column with the
vocabulary CHECK; 0259 closes the gap with
``ck_pipeline_snapshots_max_autonomy_level``. Runs against the migrated
testcontainer (real Postgres): an out-of-vocabulary ceiling must be rejected
by the DATABASE, and every in-vocabulary value plus NULL must be accepted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_SNAPSHOT_INSERT = (
    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, "
    "max_autonomy_level, graph_json, connector_bindings_json, schema_pins_json, "
    "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
    "VALUES (:id, :pid, :oid, :version, :ceiling, '{}'::json, '[]'::json, '[]'::json, "
    "'[]'::json, '[]'::json, '{}'::json, '{}'::json)"
)

_VALID_CEILINGS = ("manual_approval", "notify_on_complete", "fully_autonomous")


async def _insert_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    """Minimal committed pipelines row (own row — never the shared fixture)."""
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level) "
                "VALUES (:id, :oid, :name, 10, 30, 300, '{}'::json, '[]'::json, 'manual_approval')",
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": f"snapshot-check-{pipeline_id.hex[:8]}"},
        )
    return pipeline_id


async def _insert_snapshot(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    ceiling: str | None,
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(_SNAPSHOT_INSERT),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                # Each test owns its pipeline row, so version 1 is always free.
                "version": 1,
                "ceiling": ceiling,
            },
        )
    return snapshot_id


async def _cleanup(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
            {"pid": str(pipeline_id)},
        )
        await conn.execute(
            text("DELETE FROM pipelines WHERE id = :id"),
            {"id": str(pipeline_id)},
        )


async def test_out_of_vocabulary_snapshot_ceiling_is_rejected_by_the_db(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    """Postgres itself rejects an invalid snapshot ceiling (CHECK violation)."""
    pipeline_id = await _insert_pipeline(db_engine, test_org)
    try:
        # DBAPIError (not IntegrityError) on purpose: asyncpg raises
        # CheckViolationError and SQLAlchemy's asyncpg errmap decides the
        # wrapper class — the CONSTRAINT NAME in the message is the real
        # evidence that 0259's CHECK fired rather than some other guard.
        with pytest.raises(DBAPIError) as excinfo:
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(_SNAPSHOT_INSERT),
                    {
                        "id": str(uuid.uuid4()),
                        "pid": str(pipeline_id),
                        "oid": str(test_org),
                        "version": 1,
                        "ceiling": "banana",
                    },
                )
        message = str(excinfo.value)
        assert "ck_pipeline_snapshots_max_autonomy_level" in message, message
        assert "violates check constraint" in message.lower(), message
        # The rejected row must not have been persisted.
        async with db_engine.connect() as conn:
            remaining = (
                await conn.execute(
                    text("SELECT count(*) FROM pipeline_snapshots WHERE pipeline_id = :pid"),
                    {"pid": str(pipeline_id)},
                )
            ).scalar_one()
        assert remaining == 0
    finally:
        await _cleanup(db_engine, pipeline_id)


@pytest.mark.parametrize("ceiling", [*_VALID_CEILINGS, None])
async def test_valid_snapshot_ceiling_is_accepted(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    ceiling: str | None,
) -> None:
    """Every vocabulary value (and NULL = inherit the default) round-trips."""
    pipeline_id = await _insert_pipeline(db_engine, test_org)
    try:
        snapshot_id = await _insert_snapshot(db_engine, test_org, pipeline_id, ceiling=ceiling)
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT max_autonomy_level FROM pipeline_snapshots WHERE id = :id"),
                    {"id": str(snapshot_id)},
                )
            ).scalar_one()
        assert stored == ceiling
    finally:
        await _cleanup(db_engine, pipeline_id)
