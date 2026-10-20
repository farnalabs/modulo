"""FAR-1649 qa iterations 1+2+3 — integration: node-STARTED rows are never
re-EXECUTED unless the reset that made them ``pending`` gated it, and are
never stranded.

Drives the REAL ``dispatcher_reconcile`` tick (real Postgres + real Redis)
with a ``dispatch_phase='first_node_dispatched'`` run. Iteration 1 covered the
running + stale-heartbeat shape (the 2026-10-10 outage, where the generic
stale-heartbeat branch used to re-dispatch the run as ``execute_run`` and
re-execute the started node) — that exclusion STAYS. Iteration 2 additionally
excluded the ``pending`` re-dispatch leg; iteration 3 (this change) RESTORES
it: a fenced pending-reset keeps ``dispatcher``/``dispatched_at`` and the
phase, and with the pending leg excluded nothing else can reach a
``pending``+``saq`` row (stale sweep arms are dispatcher/capacity/running-
scoped, ``resume_run``'s claim SQL rejects ``pending``) — so a lost SAQ retry
job stranded the run ``pending`` forever. The leg is safe to restore because
the reset's only producer is idempotency-gated. The NULL-phase controls with
the identical shapes MUST still be re-dispatched, proving the RUNNING
exclusion discriminates on the phase alone.

FAR-1999 later bounded this class's run-slot lifetime: a zero-progress
node-started run past its durable node deadline is now TERMINAL-FAILED by
``_fail_node_deadline_run`` (``node_deadline_exceeded``) instead of being held
``running`` indefinitely. That is still "never re-executed" — the reaper's
write is terminal-only — and the running-shape test below asserts both halves.
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
    status: str = "running",
) -> tuple[uuid.UUID, uuid.UUID]:
    """A SAQ run (default running), stale heartbeat, no SAQ job, given phase."""
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
                    "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', :status, 'hash', :thread, :rn, 'saq', 1, "
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
                    "status": status,
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


async def _run_row_state(db_engine: Any, run_id: uuid.UUID) -> tuple[str, str | None, str | None]:
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT status, claim_token, error_code FROM runs WHERE id=:rid"), {"rid": str(run_id)}
                )
            ).first()
    finally:
        await eng.dispose()
    assert row is not None
    return row[0], row[1], row[2]


async def test_node_started_stale_run_is_not_redispatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """A first-node-STARTED run with a stale heartbeat, no SAQ job and its
    durable node deadline provably passed is NOT re-dispatched: nothing is
    enqueued and nothing claims the row.

    FAR-1649's property is unchanged — a STARTED node is never RE-EXECUTED.
    Since FAR-1999 this shape is TERMINAL-FAILED rather than left ``running``:
    ``_fail_node_deadline_run`` reaps it as ``node_deadline_exceeded`` once the
    node deadline (``dispatch_phase_entered_at`` — here the attempt-clock
    fallback, ``max(dispatched_at, started_at)`` + setup grace + node budget)
    has passed, and it is explicitly "Terminal-fail ONLY — never a
    re-dispatch". A terminal-fail is not a re-execution: a re-dispatch would
    create an SAQ job and claim the row, both of which the assertions below
    forbid. The reaper's own contract is unit-covered in
    ``tests/unit/cron_helpers/test_far1999_node_deadline_reaper.py``.
    """
    run_id, _ = await _seed_node_phase_run(db_engine, test_org, test_user, dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)

    await ch.dispatcher_reconcile()

    # FAR-1649: nothing re-enqueued the started node.
    assert not await _job_exists(saq_settings_env, run_id)
    status, claim_token, error_code = await _run_row_state(db_engine, run_id)
    # FAR-1999: zero-progress node-started past its deadline is terminal —
    # never left running, never reset to pending.
    assert status == "failed"
    assert error_code == "node_deadline_exceeded"
    # Terminal columns only: nothing claimed or re-enqueued the row.
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
    status, _claim_token, _error_code = await _run_row_state(db_engine, run_id)
    assert status == "running"


async def test_pending_node_started_reset_run_is_redispatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """FAR-1649 qa iteration 3 (no-strand regression): a node-STARTED run
    RESET to pending (the fenced pending-reset keeps
    ``dispatcher``/``dispatched_at``/the phase) and whose SAQ job is GONE is
    re-dispatched as ``execute_run`` by the reenqueue-window leg.

    Iteration 2 excluded this shape, which made it invisible to EVERY
    recovery path and permanently stranded it ``pending`` when the retry job
    was lost. The restore is safe because the reset's only producer is
    idempotency-gated at all three call sites — so the re-execution this
    enqueues runs a graph already vetted safe to re-run (FAR-295). The
    RUNNING node-started row (test above) still must NOT be re-dispatched:
    that exclusion is what fixes the 2026-10-10 storm."""
    run_id, _ = await _seed_node_phase_run(
        db_engine,
        test_org,
        test_user,
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
        status="pending",
    )

    await ch.dispatcher_reconcile()

    assert await _job_exists(saq_settings_env, run_id)
    status, claim_token, _error_code = await _run_row_state(db_engine, run_id)
    # Re-enqueued only — the reconcile tick never claims the row itself.
    assert status == "pending"
    assert claim_token == "token-a"


async def test_control_pending_null_phase_run_is_redispatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """The control: the IDENTICAL pending row with no durable phase IS
    re-dispatched (as ``execute_run``) by the reenqueue-window leg — pending
    recovery does not discriminate on the phase (iteration 3); the phase
    discrimination lives on the RUNNING legs above."""
    run_id, _ = await _seed_node_phase_run(db_engine, test_org, test_user, dispatch_phase=None, status="pending")

    await ch.dispatcher_reconcile()

    assert await _job_exists(saq_settings_env, run_id)
