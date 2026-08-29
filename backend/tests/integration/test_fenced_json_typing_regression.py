"""Regression test for the fenced run-status JSON-typing fix (PR #2165).

``update_run_status``'s fenced path (claim_token set) writes the four JSON run
columns with a single conditional UPDATE. The SQL must cast those params to
``json`` -- NOT ``jsonb``:

* On a fully-migrated DB the columns are ``jsonb`` and the implicit json->jsonb
  up-cast makes either cast work.
* On a DB where the columns are still typed ``json`` (the partial/halted
  migration state this deploy-fix targets), the pre-fix ``CAST(:param AS
  jsonb)`` raised ``column ... is of type json but expression is of type
  jsonb`` and the fenced terminal write failed.

This test forces the four columns back to ``json`` (the migrations promote them
to ``jsonb``) and exercises the fenced write. It must pass on the fixed code and
FAIL on the pre-fix code (which still casts to ``jsonb``). The column types are
restored to ``jsonb`` afterwards so the shared test DB is left untouched for
other tests.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import update_run_status
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

_RUN_JSON_COLUMNS = (
    "outputs_json",
    "node_telemetry_json",
    "node_token_usage",
    "cost_breakdown",
)


async def _insert_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str = "running",
    claim_token: str | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    values: dict[str, object] = {
        "id": run_id,
        "organisation_id": org_id,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "trigger_type": "manual",
        "status": status,
        "input_hash": uuid.uuid4().hex,
        "langgraph_thread_id": f"thread-{run_id.hex[:16]}",
        "run_number": int(run_id.int % 10**9) + 1,
    }
    if claim_token is not None:
        values["claim_token"] = claim_token
    await session.execute(insert(Run).values(**values))
    return run_id


class TestFencedJsonTyping:
    async def test_fenced_write_succeeds_on_json_typed_columns(
        self,
        db_session: AsyncSession,
        test_org: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
    ) -> None:
        claim_token = "tok-json-typing-regression"
        run_id = await _insert_run(
            db_session,
            org_id=test_org,
            pipeline_id=test_pipeline,
            snapshot_id=test_snapshot,
            status="complete",
            claim_token=claim_token,
        )
        await db_session.commit()

        # Reproduce the partial/halted-migration state: force the four JSON run
        # columns back to ``json`` (the migrations promote them to ``jsonb``).
        async with db_session.begin():
            for col in _RUN_JSON_COLUMNS:
                await db_session.execute(text(f"ALTER TABLE runs ALTER COLUMN {col} TYPE json USING {col}::json"))

        try:
            # The fenced write must accept json-typed columns (current fix). On
            # the pre-fix SQL (CAST(... AS jsonb)) this raises
            # "column ... is of type json but expression is of type jsonb".
            outputs = {"node_a": {"result": "ok"}}
            node_usage = {"n1": {"total_tokens": 1000, "input_tokens": 1000, "output_tokens": 0}}
            telemetry = {"n1": {"wall_clock_time_ms": 12, "exit_code": 0}}
            cost_breakdown = [{"name": "llm_tokens", "amount_usd": "0.01"}]

            result = await update_run_status(
                db_session,
                run_id,
                "complete",
                claim_token=claim_token,
                outputs_json=outputs,
                node_token_usage=node_usage,
                node_telemetry_json=telemetry,
                cost_breakdown=cost_breakdown,
            )
            assert result is not None, "the fenced write must succeed on json-typed columns"

            await db_session.commit()

            # Confirm the values were actually persisted to the json-typed
            # columns (a raw read sidesteps any ORM/identity-map staleness).
            row = (
                await db_session.execute(
                    text(
                        "SELECT outputs_json, node_token_usage, node_telemetry_json, "
                        "cost_breakdown FROM runs WHERE id = :rid"
                    ),
                    {"rid": str(run_id)},
                )
            ).first()
            assert row is not None
            assert row[0] == outputs, "outputs_json must be persisted"
            assert row[1] == node_usage, "node_token_usage must be persisted"
            assert row[2] == telemetry, "node_telemetry_json must be persisted"
            assert row[3] == cost_breakdown, "cost_breakdown must be persisted"
        finally:
            # Restore the migrated (jsonb) column types so other tests see the
            # state the migrations produce.
            async with db_session.begin():
                for col in _RUN_JSON_COLUMNS:
                    await db_session.execute(text(f"ALTER TABLE runs ALTER COLUMN {col} TYPE jsonb USING {col}::jsonb"))
