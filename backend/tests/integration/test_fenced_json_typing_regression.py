"""Regression test for the deploy fix (PR #2167).

Reproduces the partial/halted-migration state the fix targets: the ``runs``
JSON columns are still typed plain ``json`` (not promoted to ``jsonb``) while
``update_run_status``'s fenced path writes through raw SQL. The fenced write
casts the bound JSON param to ``json`` (not ``jsonb``), so it must keep working
against plain ``json`` columns — type-agnostic across both column types.

The test force-downgrades the four run JSON columns to ``json``, exercises the
fenced ``update_run_status`` write, and asserts the values round-trip — then
restores the columns to ``jsonb`` in a ``finally`` so the shared test DB is left
untouched for other integration tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import insert as sa_insert
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.db.crud.run import update_run_status
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

# The four generic-JSON run columns the deploy's json/jsonb promotion targets.
_JSON_COLUMNS = ("outputs_json", "node_telemetry_json", "node_token_usage", "cost_breakdown")


def _as_json(value: Any) -> Any:
    """Normalise a json/jsonb column read the asyncpg codec may hand back as a
    JSON string (plain ``json`` uses the py codec; be agnostic to the codec
    shape) into the Python object it represents."""
    if isinstance(value, str):
        return json.loads(value)
    return value


async def _set_run_json_column_type(db_engine: AsyncEngine, col_type: str) -> None:
    """Force the four run JSON columns to the given SQL type (``json``/``jsonb``).

    Runs on a dedicated ``db_engine`` connection with an explicit begin/commit so
    the DDL survives the function-scoped ``db_session`` rollback and takes
    effect within the same DB the fenced write executes against.

    The ALTER TABLE requires an ACCESS EXCLUSIVE lock on ``runs``; under parallel
    execution (``-n 2``) another worker may briefly hold a conflicting lock. Set a
    short ``lock_timeout`` and retry instead of blocking until the per-test
    pytest timeout fires (observed 300s hang).
    """
    async with db_engine.connect() as conn:
        for column in _JSON_COLUMNS:
            for _attempt in range(30):
                try:
                    async with conn.begin():
                        await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                        await conn.execute(
                            text(f"ALTER TABLE runs ALTER COLUMN {column} TYPE {col_type} USING {column}::{col_type}")
                        )
                    break
                except Exception:
                    if _attempt == 29:
                        raise
                    await asyncio.sleep(1)


async def test_fenced_update_run_status_succeeds_on_plain_json_columns(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """The fenced status write must land (and round-trip) when the runs JSON
    columns are still plain ``json`` — the partial/halted-migration state the
    deploy fix targets."""
    run_id = uuid.uuid4()
    claim_token = "tok-plain-json"
    outputs_json = {"node_a": {"result": "ok"}}
    node_token_usage = {"n1": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    node_telemetry_json = {"n1": {"status": "ok", "wall_clock_time_ms": 100}}
    cost_breakdown = [{"component": "llm_tokens", "amount": "0.01", "kind": "calculated"}]

    # Seed the run row in the shared test org.
    await db_session.execute(
        sa_insert(Run).values(
            id=run_id,
            organisation_id=test_org,
            pipeline_id=test_pipeline,
            snapshot_id=test_snapshot,
            trigger_type="manual",
            status="running",
            input_hash=uuid.uuid4().hex,
            langgraph_thread_id=f"thread-{run_id.hex[:16]}",
            run_number=int(run_id.int % 10**9) + 1,
            claim_token=claim_token,
        )
    )
    await db_session.commit()

    try:
        # Downgrade the four JSON columns to plain ``json``.
        await _set_run_json_column_type(db_engine, "json")

        async with db_session.begin():
            await db_session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(test_org)}
            )
            result = await update_run_status(
                db_session,
                run_id,
                "complete",
                claim_token=claim_token,
                outputs_json=outputs_json,
                node_token_usage=node_token_usage,
                node_telemetry_json=node_telemetry_json,
                cost_breakdown=cost_breakdown,
            )
        assert result is not None, "the fenced write must land against plain-json columns"
        assert result.status == "complete"

        # Read the persisted row back and assert the four values round-tripped.
        row = (
            await db_session.execute(
                text(
                    "SELECT outputs_json, node_telemetry_json, node_token_usage, cost_breakdown "
                    "FROM runs WHERE id = :rid"
                ),
                {"rid": str(run_id)},
            )
        ).first()
        assert row is not None
        assert _as_json(row[0]) == outputs_json
        assert _as_json(row[1]) == node_telemetry_json
        assert _as_json(row[2]) == node_token_usage
        assert _as_json(row[3]) == cost_breakdown
    finally:
        # Restore the shared test DB to jsonb so other integration tests are
        # unaffected.
        await _set_run_json_column_type(db_engine, "jsonb")
