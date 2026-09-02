"""Integration tests for fire_due_triggers (plan F1) - real Redis + Postgres.

The multi-machine safety invariant is tested by running TWO concurrent
``fire_due_triggers()`` invocations (simulating two machines' ticks) against one
due row: the atomic ``UPDATE ... WHERE next_fire_at <= now() RETURNING id``
makes exactly ONE of them win the epoch, so exactly ONE fire job is enqueued
and enqueue-count == RETURNING-returned-count.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from modulo.core import cron_helpers as ch

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_due_cron_trigger(
    db_engine: Any, org_id: uuid.UUID, account_id: uuid.UUID, cron_expression: str = "*/30 * * * * *"
) -> uuid.UUID:
    from sqlalchemy import NullPool, text
    from sqlalchemy.ext.asyncio import create_async_engine

    # Fresh NullPool engine per seed -” the shared session-scoped db_engine's
    # pool can carry a pending asyncpg operation between session-loop coroutines.
    url = db_engine.url.render_as_string(hide_password=False)
    eng = create_async_engine(url, poolclass=NullPool)
    try:
        pipeline_id = uuid.uuid4()
        trigger_id = uuid.uuid4()
        # Unique per seed: the shared session org + uq_pipelines_org_name make a
        # fixed name collide with every later seed in the same pytest session.
        pipeline_name = f"saq-test-{uuid.uuid4().hex[:10]}"
        async with eng.connect() as conn, conn.begin():
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
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, account_id, trigger_type, active, "
                    "max_concurrent_runs, config_json, cron_expression, next_fire_at) "
                    "VALUES (:id, :oid, :pid, :uid, 'cron', true, 5, '{}'::json, :cron, now() - interval '1 second')"
                ),
                {
                    "id": str(trigger_id),
                    "oid": str(org_id),
                    "pid": str(pipeline_id),
                    "uid": str(account_id),
                    "cron": cron_expression,
                },
            )
        return trigger_id
    finally:
        await eng.dispose()


async def _queue_jobs(redis_url: str, queue_name: str) -> list[dict]:
    from redis import asyncio as aioredis

    r = aioredis.from_url(redis_url)
    try:
        job_ids = await r.lrange(f"saq:{queue_name}:queued", 0, -1)
        return [jid.decode() if isinstance(jid, bytes) else str(jid) for jid in job_ids]
    finally:
        await r.aclose()


async def _count_trigger_jobs(redis_url: str, trigger_id: uuid.UUID) -> int:
    jobs = await _queue_jobs(redis_url, "runs")
    return len([j for j in jobs if j.startswith(f"saq:job:runs:fire:{trigger_id}:")])


@pytest.mark.asyncio
async def test_two_concurrent_ticks_enqueue_exactly_one_fire_job(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    # Yearly cron: the advanced next_fire_at lands a year out, so a straggler
    # tick in the same test window can never legitimately re-fire it.
    trigger_id = await _seed_due_cron_trigger(db_engine, test_org, test_user, cron_expression="0 0 1 1 *")

    results = await asyncio.gather(
        ch.fire_due_triggers(),
        ch.fire_due_triggers(),
    )

    total_enqueued = sum(r["cron_enqueued"] for r in results)
    # Exactly ONE epoch win despite two concurrent ticks.
    assert total_enqueued == 1
    assert await _count_trigger_jobs(saq_settings_env, trigger_id) == 1


@pytest.mark.asyncio
async def test_next_tick_fires_again_after_first_epoch_consumed(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """After the first epoch's advance, the same row is not due again until the
    advanced next_fire_at elapses - a follow-up tick must NOT double-fire."""
    trigger_id = await _seed_due_cron_trigger(db_engine, test_org, test_user, cron_expression="0 0 1 1 *")

    first = await ch.fire_due_triggers()
    assert first["cron_enqueued"] >= 1

    await ch.fire_due_triggers()  # second tick
    # The second tick must NOT enqueue another fire for THIS trigger.
    assert await _count_trigger_jobs(saq_settings_env, trigger_id) == 1


@pytest.mark.asyncio
async def test_paused_org_skips_enqueue_but_advances_next_fire(
    saq_settings_env: str, db_engine: Any, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """Org-wide pause: SKIP-not-defer — zero fire jobs enqueued for the paused
    org but next_fire_at still advances (no catch-up storm on unpause)."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    trigger_id = await _seed_due_cron_trigger(db_engine, test_org, test_user, cron_expression="0 0 1 1 *")

    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE organisations SET triggers_paused = TRUE, triggers_paused_at = now() WHERE id = :oid"),
            {"oid": str(test_org)},
        )
    try:
        summary = await ch.fire_due_triggers()

        assert summary["cron_enqueued"] == 0
        assert summary["cron_skipped_paused"] >= 1
        assert await _count_trigger_jobs(saq_settings_env, trigger_id) == 0

        # SKIP-not-defer: the epoch was consumed — next_fire_at advanced past now.
        async with db_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT next_fire_at FROM triggers WHERE id = :tid"),
                {"tid": str(trigger_id)},
            )
            next_fire_at = row.scalar_one()
            assert next_fire_at is not None
            assert next_fire_at > datetime.now(UTC)
    finally:
        # test_org is session-scoped and shared by every integration test; leave
        # it unpaused or downstream handle_webhook tests hit TriggersPausedError.
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE organisations SET triggers_paused = FALSE, triggers_paused_at = NULL WHERE id = :oid"),
                {"oid": str(test_org)},
            )
