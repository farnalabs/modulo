"""FAR-1649 qa iteration 1 — integration: node-STARTED rows are never re-dispatched.

Drives the REAL ``dispatcher_reconcile`` tick (real Postgres + real Redis) with
a ``dispatch_phase='first_node_dispatched'`` run whose heartbeat is stale and
whose SAQ job is gone — the exact production shape (2026-10-10 outage) where
the generic stale-heartbeat branch used to re-dispatch the run as
``execute_run`` and re-execute the started node. The run must NOT be
re-dispatched; the NULL-phase control with the identical shape MUST be, proving
the exclusion discriminates on the phase.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from modulo.core import cron_helpers as ch
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_node_phase_run(
    db_engine: Any,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    dispatch_phase: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A running SAQ run, stale heartbeat, no SAQ job, given durable phase."""
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    pipeline_name = f"node-phase-reconcile-{uuid.uuid4().hex[:10]}"
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn, conn.begin():
            await conn.execute(text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS enqueue_failed_at timestamptz"))
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, graph_nodes_json, "
                    "run_context_defaults, visibility, max_concurrent_runs) "
                    "VALUES (:id, :oid, :uid, :pname, '[]'::json, '{}'::json, 'org', 5)"
                ),
                {"id": str(pipeline_id), "oid": str(org_id), "uid": str(account_id), "pname": pipeline_name},
            )
            await conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (id, organisation_id, pipeline_id, snapshot_version, "
                    "account_id, graph_json, connector_bindings_json, schema_pins_json, prompt_pins_json, "
                    "model_backend_pins_json, composite_bindings_json, run_context_defaults) "
                    "VALUES (:id, :oid, :pid, 1, :uid, '{}'::json, '[]'::json, '[]'::json, '[]'::json, "
                    "'[]'::json, '[]'::json, '{}'::json)"
                ),
                {"id": str(snapshot_id), "oid": str(org_id), "pid": str(pipeline_id), "uid": str(account_id)},
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, trigger_type, "
                    "status, input_hash, langgraph_thread_id, run_number, dispatcher, claim_count, "
                    "heartbeat_at, dispatched_at, saq_job_id, claim_token, started_at, dispatch_phase) "
                    "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', 'running', 'hash', :thread, :rn, 'saq', 1, "
                    "now() - interval '30 minutes', now() - interval '35 minutes', "
                    ":job_id, 'token-a', now() - interval '60 minutes', :phase)"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org_id),
                    "pid": str(pipeline_id),
                    "sid": str(snapshot_id),
                    "uid": str(account_id),
                    "thread": f"{org_id}:{run_id}",
                    "rn": run_number,
                    "job_id": f"saq:job:runs:run:{run_id}",
                    "phase": dispatch_phase,
                },
            )
        return run_id, pipeline_id
    finally:
        await eng.dispose()


async def _job_exists(redis_url: str, run_id: uuid.UUID) -> bool:
    from redis import asyncio as aioredis

    r = aioredis.from_url(redis_url)
    try:
        return len(await r.keys(f"saq:job:runs:run:{run_id}*")) > 0
    finally:
        await r.aclose()


async def _run_row_state(db_engine: Any, run_id: uuid.UUID) -> tuple[str, str | None]:
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(text("SELECT status, claim_token FROM runs WHERE id=:rid"), {"rid": str(run_id)})
            ).first()
    finally:
        await eng.dispose()
    assert row is not None
    return row[0], row[1]


async def test_node_started_stale_run_is_not_redispatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """A first-node-STARTED run with a stale heartbeat and no SAQ job is NOT
    re-dispatched: no job appears, the row stays running, the claim token is
    untouched (nothing claimed or re-enqueued it)."""
    run_id, _ = await _seed_node_phase_run(db_engine, test_org, test_user, dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)

    await ch.dispatcher_reconcile()

    assert not await _job_exists(saq_settings_env, run_id)
    status, claim_token = await _run_row_state(db_engine, run_id)
    assert status == "running"
    assert claim_token == "token-a"


async def test_control_null_phase_stale_run_is_redispatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """The control: the IDENTICAL row with no durable phase (no node-start
    evidence) IS re-dispatched by the tick — the exclusion discriminates on
    the phase alone."""
    run_id, _ = await _seed_node_phase_run(db_engine, test_org, test_user, dispatch_phase=None)

    await ch.dispatcher_reconcile()

    assert await _job_exists(saq_settings_env, run_id)
    status, _claim_token = await _run_row_state(db_engine, run_id)
    assert status == "running"
