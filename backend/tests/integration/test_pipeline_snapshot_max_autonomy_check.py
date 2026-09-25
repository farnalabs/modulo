"""FAR-1223: ``pipeline_snapshots`` autonomy columns are CHECK-guarded.

Migration 0256 added ``max_autonomy_level`` to BOTH ``pipelines`` and
``pipeline_snapshots`` but only guarded the ``pipelines`` column with the
vocabulary CHECK; ``pipeline_snapshots.default_autonomy_level`` has been
unguarded since 0110 (the live ``pipelines`` sibling has been guarded by
``ck_pipelines_autonomy_level`` since 0003). Migration 0259 closes BOTH gaps:

* ``ck_pipeline_snapshots_max_autonomy_level``
* ``ck_pipeline_snapshots_default_autonomy_level``

Runs against the migrated testcontainer (real Postgres): an out-of-vocabulary
value must be rejected by the DATABASE (the constraint name in the error is
the evidence the right CHECK fired), and every in-vocabulary value plus NULL
must be accepted — both snapshot columns are nullable, so NULL must round-trip.
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
    "max_autonomy_level, default_autonomy_level, graph_json, connector_bindings_json, schema_pins_json, "
    "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
    "VALUES (:id, :pid, :oid, :version, :ceiling, :default, '{}'::json, '[]'::json, '[]'::json, "
    "'[]'::json, '[]'::json, '{}'::json, '{}'::json)"
)

_VALID_LEVELS = ("manual_approval", "notify_on_complete", "fully_autonomous")
#: Omitting a nullable column and writing an explicit NULL are the same row to
#: Postgres (the column has no DEFAULT), so NULL is exercised either way.
_NULL = None

# Literal statements per target column — no f-string SQL (S608) and no
# ambiguity about which constraint the INSERT is aiming at.
_BAD_VALUE_INSERTS = {
    "max_autonomy_level": (
        "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
        "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
        "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json, "
        "max_autonomy_level) "
        "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, "
        "'[]'::json, '[]'::json, '{}'::json, '{}'::json, :value)"
    ),
    "default_autonomy_level": (
        "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
        "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
        "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json, "
        "default_autonomy_level) "
        "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, "
        "'[]'::json, '[]'::json, '{}'::json, '{}'::json, :value)"
    ),
}


async def _insert_pipeline(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """Minimal committed pipelines row (own row — never the shared fixture).

    ``pipelines.account_id`` is a NOT NULL FK to ``accounts``, so a valid
    account is required — the session-scoped ``test_user`` fixture supplies it.
    """
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, 'manual_approval')",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "name": f"snapshot-check-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _insert_snapshot(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    ceiling: str | None,
    default: str | None = _NULL,
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
                "default": default,
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


async def _count_snapshots(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> int:
    async with db_engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pipeline_snapshots WHERE pipeline_id = :pid"),
                    {"pid": str(pipeline_id)},
                )
            ).scalar_one()
        )


@pytest.mark.parametrize(
    ("column", "bad_value"),
    [
        ("max_autonomy_level", "banana"),
        ("default_autonomy_level", "banana"),
    ],
)
async def test_out_of_vocabulary_snapshot_value_is_rejected_by_the_db(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    column: str,
    bad_value: str,
) -> None:
    """Postgres itself rejects an invalid snapshot value (CHECK violation)."""
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    constraint = f"ck_pipeline_snapshots_{column}"
    try:
        # DBAPIError (not IntegrityError) on purpose: asyncpg raises
        # CheckViolationError and SQLAlchemy's asyncpg errmap decides the
        # wrapper class — the CONSTRAINT NAME in the message is the real
        # evidence that 0259's CHECK fired rather than some other guard.
        with pytest.raises(DBAPIError) as excinfo:
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(_BAD_VALUE_INSERTS[column]),
                    {
                        "id": str(uuid.uuid4()),
                        "pid": str(pipeline_id),
                        "oid": str(test_org),
                        "value": bad_value,
                    },
                )
        message = str(excinfo.value)
        assert constraint in message, message
        assert "violates check constraint" in message.lower(), message
        # The rejected row must not have been persisted.
        assert await _count_snapshots(db_engine, pipeline_id) == 0
    finally:
        await _cleanup(db_engine, pipeline_id)


@pytest.mark.parametrize("value", [*_VALID_LEVELS, None])
async def test_valid_snapshot_max_autonomy_level_is_accepted(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    value: str | None,
) -> None:
    """Every ceiling vocabulary value (and NULL = inherit the default) round-trips."""
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    try:
        snapshot_id = await _insert_snapshot(db_engine, test_org, pipeline_id, ceiling=value)
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT max_autonomy_level FROM pipeline_snapshots WHERE id = :id"),
                    {"id": str(snapshot_id)},
                )
            ).scalar_one()
        assert stored == value
    finally:
        await _cleanup(db_engine, pipeline_id)


@pytest.mark.parametrize("value", [*_VALID_LEVELS, None])
async def test_valid_snapshot_default_autonomy_level_is_accepted(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    value: str | None,
) -> None:
    """Every default vocabulary value (and NULL = nothing frozen) round-trips.

    NULL must stay legal: ``pipeline_snapshots.default_autonomy_level`` is
    nullable by design (0110 dropped NOT NULL) and every other integration
    INSERT in the suite omits the column.
    """
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user)
    try:
        snapshot_id = await _insert_snapshot(
            db_engine,
            test_org,
            pipeline_id,
            ceiling=None,
            default=value,
        )
        async with db_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text("SELECT default_autonomy_level FROM pipeline_snapshots WHERE id = :id"),
                    {"id": str(snapshot_id)},
                )
            ).scalar_one()
        assert stored == value
    finally:
        await _cleanup(db_engine, pipeline_id)
