"""Integration test: stale SAQ unique-cron registrations are reconciled on startup.

Reproduces FAR-356 — a unique cron's persisted Redis registration
(``cron:<qualname>`` in the job hash, ``incomplete`` zset, and ``queued`` list)
survives a worker restart and silently locks the cron at its OLD schedule when
the expression changes in code after first registration. ``reconcile_cron_registrations``
runs once at system-worker startup and clears each unique cron's stale registration
so SAQ's ``schedule()`` loop re-enqueues it with the CURRENT cadence.

Uses the real RedisContainer + migrated Postgres from ``conftest.py``
(``saq_settings_env`` / ``saq_redis_url``).
"""

from __future__ import annotations

import redis.asyncio as aioredis
from saq import CronJob
from saq.queue.redis import RedisQueue

import modulo.core.saq_worker as sw
from modulo.core.saq_worker import reconcile_cron_registrations


async def my_dummy_cron(*_a: object, **_k: object) -> None:  # pragma: no cover - test fixture
    return None


class TestReconcileCronRegistrations:
    async def test_stale_unique_cron_cleared_and_non_unique_untouched(
        self, saq_settings_env: str, saq_redis_url: str
    ) -> None:
        redis_client = aioredis.from_url(saq_redis_url, socket_connect_timeout=10)
        q = RedisQueue(redis_client, name="system")

        # --- seed a STALE unique-cron registration ---------------------------
        stale_key = "cron:my_dummy_cron"
        stale_jid = q.job_id(stale_key)
        incomplete = q.namespace("incomplete")
        queued = q.namespace("queued")
        await redis_client.set(stale_jid, b"{}")
        await redis_client.zadd(incomplete, {stale_jid: 9999999999.0})
        await redis_client.lpush(queued, stale_jid)

        # --- seed a NON-unique cron registration that must survive -----------
        other_key = "cron:my_other_cron"
        other_jid = q.job_id(other_key)
        await redis_client.set(other_jid, b"{}")
        await redis_client.zadd(incomplete, {other_jid: 9999999999.0})
        await redis_client.lpush(queued, other_jid)

        unique_job = CronJob(function=my_dummy_cron, cron="* * * * *", unique=True)
        non_unique_job = CronJob(function=my_dummy_cron, cron="* * * * *", unique=False)

        await reconcile_cron_registrations(q, [unique_job, non_unique_job])

        # Stale UNIQUE registration fully cleared.
        assert await redis_client.exists(stale_jid) == 0
        assert await redis_client.zscore(incomplete, stale_jid) is None
        assert await redis_client.lpos(queued, stale_jid) is None

        # NON-unique registration left untouched.
        assert await redis_client.exists(other_jid) == 1
        assert await redis_client.zscore(incomplete, other_jid) is not None
        assert await redis_client.lpos(queued, other_jid) is not None

        # The function is reachable on the module (import smoke).
        assert sw.reconcile_cron_registrations is reconcile_cron_registrations
