"""Integration tests for dispatcher_reconcile (plan F3c) - real Redis + Postgres.

Positive path: a staled dispatched run whose SAQ job hash was evicted is
re-dispatched by reconcile (DEL abort key + ZREM incomplete + LREM queued +
normal enqueue). Re-dispatch discriminator: awaiting_human -> resume_run.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from modulo.core import cron_helpers as ch

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_saq_run(
    db_engine: Any,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    status: str = "running",
    heartbeat_stale: bool = True,
    dispatched: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    run_number = int(run_id.int % 10**9) + 1
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, graph_nodes_json, "
                    "run_context_defaults, visibility, max_concurrent_runs) "
                    "VALUES (:id, :oid, :uid, 'saq-reconcile-test', '[]'::json, '{}'::json, 'org', 5)"
                ),
                {"id": str(pipeline_id), "oid": str(org_id), "uid": str(account_id)},
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
                    "heartbeat_at, dispatched_at, saq_job_id, claim_token) "
                    "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', :status, 'hash', :thread, :rn, 'saq', 1, "
                    "CASE WHEN :stale THEN now() - interval '30 minutes' ELSE now() END, "
                    "CASE WHEN :dispatched THEN now() - interval '30 minutes' ELSE NULL END, "
                    ":job_id, 'token-a')"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org_id),
                    "pid": str(pipeline_id),
                    "sid": str(snapshot_id),
                    "uid": str(account_id),
                    "status": status,
                    "thread": f"{org_id}:{run_id}",
                    "rn": run_number,
                    "stale": heartbeat_stale,
                    "dispatched": dispatched,
                    "job_id": f"saq:job:runs:run:{run_id}",
                },
            )
        return run_id, pipeline_id
    finally:
        await eng.dispose()


async def _job_exists(redis_url: str, job_key: str) -> bool:
    from redis import asyncio as aioredis

    r = aioredis.from_url(redis_url)
    try:
        return await r.exists(f"saq:job:runs:{job_key}") == 1
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_staled_running_run_with_evicted_job_is_redistpatched(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reconcile re-dispatches through dispatch_run; the SAQ path is the one under
    # test (shadow routes execute_run to Celery, which creates no SAQ job).
    monkeypatch.setenv("SAQ_ENABLED", "true")
    from redis import asyncio as aioredis
    from saq.queue.redis import RedisQueue

    run_id, _ = await _seed_saq_run(db_engine, test_org, test_user, status="running")

    # Simulate a partial eviction: a normal enqueue whose hash was then deleted,
    # leaving the incomplete zset member behind.
    redis_client = aioredis.from_url(saq_settings_env)
    try:
        q = RedisQueue(redis_client, name="runs")
        job = await q.enqueue("modulo.core.saq_worker.execute_run", key=f"run:{run_id}")
        assert job is not None
        await redis_client.delete(job.id)  # evict the job hash
    finally:
        await redis_client.aclose()

    # Reconcile must repair + re-dispatch (staled heartbeat, no job).
    summary = await ch.dispatcher_reconcile()
    assert summary["scanned"] >= 1
    assert summary["repaired"] == 1

    # The job now exists again (fresh dispatch), key deterministic.
    assert await _job_exists(saq_settings_env, f"run:{run_id}")


@pytest.mark.asyncio
async def test_awaiting_human_evicted_job_redispatch_as_resume(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAQ_ENABLED", "true")
    from redis import asyncio as aioredis
    from saq.queue.redis import RedisQueue

    run_id, _ = await _seed_saq_run(db_engine, test_org, test_user, status="awaiting_human")
    redis_client = aioredis.from_url(saq_settings_env)
    try:
        q = RedisQueue(redis_client, name="runs")
        job = await q.enqueue("modulo.core.saq_worker.execute_run", key=f"run:{run_id}")
        await redis_client.delete(job.id)
    finally:
        await redis_client.aclose()

    summary = await ch.dispatcher_reconcile()
    assert summary["repaired"] == 1

    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(db_engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(text("SELECT claim_token, saq_job_id FROM runs WHERE id=:rid"), {"rid": str(run_id)})
            ).first()
    finally:
        await eng.dispose()
    # A fresh dispatch rotates the claim token (deterministic saq_job_id stays the same).
    assert row[0] != "token-a"
    assert await _job_exists(saq_settings_env, f"run:{run_id}")


@pytest.mark.asyncio
async def test_live_job_not_repaired(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAQ_ENABLED", "true")
    from redis import asyncio as aioredis
    from saq.queue.redis import RedisQueue

    run_id, _ = await _seed_saq_run(db_engine, test_org, test_user, status="running", heartbeat_stale=False)
    redis_client = aioredis.from_url(saq_settings_env)
    try:
        q = RedisQueue(redis_client, name="runs")
        await q.enqueue("modulo.core.saq_worker.execute_run", key=f"run:{run_id}")
    finally:
        await redis_client.aclose()

    summary = await ch.dispatcher_reconcile()
    assert summary["repaired"] == 0
