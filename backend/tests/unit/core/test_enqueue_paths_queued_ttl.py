"""FAR-2002 - every enqueue path bounds a still-queued job hash.

The first change applied the status-gated queued TTL only in
``core.dispatch._enqueue_saq``. Two other paths enqueue job hashes with the
same finish-origin-only TTL, so under a runs-worker wedge (with the system
worker alive) they could still consume memory up to ``maxmemory`` without
``volatile-lru`` being able to evict them:

* ``core.cron_helpers._enqueue_fire_job_async`` - the per-item trigger fire job;
* ``core.saq_worker._enqueue_suite_run_execution`` - the suite-run execution job.

Both now reuse ``core.dispatch._apply_queued_ttl``. The TTL stays status-gated:
the Lua script only expires a hash whose stored status is ``queued``, so a job
the worker has picked up (``active``) is never expired.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis as fakeredis
import pytest

import modulo.core.cron_helpers as ch
import modulo.core.saq_worker as sw
from modulo.core import dispatch


def _payload(status: str) -> str:
    return json.dumps({"function": "modulo.core.saq_worker.execute_run", "status": status})


class _OpenFakeRedis(fakeredis.FakeRedis):
    """FakeRedis whose ``aclose`` is a no-op.

    ``_enqueue_suite_run_execution`` closes its Redis client in a ``finally``;
    the assertions run after it returns, so the fake server must stay usable.
    """

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        return None


class _FireQueue:
    """Stand-in for the SAQ ``RedisQueue`` handed to the cron enqueue helper."""

    def __init__(self, redis: _OpenFakeRedis, *, status: str = "queued", dedupe: bool = False) -> None:
        self.redis = redis
        self._status = status
        self._dedupe = dedupe

    async def enqueue(self, function: str, *, key: str, **kwargs: Any) -> Any:
        job_id = self.job_id(key)
        await self.redis.set(job_id, _payload(self._status))
        if self._dedupe:
            return None
        return SimpleNamespace(id=job_id)

    def job_id(self, key: str) -> str:
        return f"saq:job:fire:{key}"


class _SuiteQueue:
    """Stand-in for the SAQ ``RedisQueue`` built inside the suite enqueue."""

    def __init__(self, redis: _OpenFakeRedis, name: str | None = None) -> None:
        self.redis = redis
        self.name = name
        self.status = "queued"

    async def enqueue(self, function: str, *, key: str, **kwargs: Any) -> Any:
        job_id = self.job_id(key)
        await self.redis.set(job_id, _payload(self.status))
        return SimpleNamespace(id=job_id)

    def job_id(self, key: str) -> str:
        return f"saq:job:{self.name}:{key}"


# ---------------------------------------------------------------------------
# Trigger-fire enqueue (core.cron_helpers)
# ---------------------------------------------------------------------------


async def test_fire_enqueue_bounds_a_queued_job_hash() -> None:
    redis = _OpenFakeRedis()
    queue = _FireQueue(redis)

    job_id = await ch._enqueue_fire_job_async(queue, "modulo.core.saq_worker.fire_cron_trigger", key="epoch-1")

    assert job_id == "saq:job:fire:epoch-1"
    assert await redis.ttl(job_id) == dispatch.SAQ_QUEUED_JOB_TTL


async def test_fire_enqueue_leaves_a_picked_up_job_alone() -> None:
    """The worker's dequeue transition to ``active`` must not acquire a TTL."""
    redis = _OpenFakeRedis()
    queue = _FireQueue(redis, status="active")

    job_id = await ch._enqueue_fire_job_async(queue, "modulo.core.saq_worker.fire_cron_trigger", key="epoch-1")

    assert job_id == "saq:job:fire:epoch-1"
    assert await redis.ttl(job_id) == -1


async def test_fire_enqueue_bounds_a_hash_deduped_by_another_machine() -> None:
    """``enqueue`` returning None means the key already existed - bound it too."""
    redis = _OpenFakeRedis()
    await redis.set("saq:job:fire:epoch-1", _payload("queued"))
    queue = _FireQueue(redis, dedupe=True)

    job_id = await ch._enqueue_fire_job_async(queue, "modulo.core.saq_worker.fire_cron_trigger", key="epoch-1")

    assert job_id is None
    assert await redis.ttl("saq:job:fire:epoch-1") == dispatch.SAQ_QUEUED_JOB_TTL


# ---------------------------------------------------------------------------
# Suite-run execution enqueue (core.saq_worker)
# ---------------------------------------------------------------------------


def _patch_suite_enqueue(monkeypatch: pytest.MonkeyPatch, queue: _SuiteQueue, redis: _OpenFakeRedis) -> None:
    monkeypatch.setattr(sw, "aioredis", SimpleNamespace(from_url=lambda *args, **kwargs: redis))
    monkeypatch.setattr("saq.queue.redis.RedisQueue", lambda *args, **kwargs: queue)
    monkeypatch.setattr(
        sw,
        "get_settings",
        lambda: SimpleNamespace(
            redis_url="redis://localhost:6379/0",
            saq_runs_queue="runs",
            saq_redis_pool_size=5,
        ),
    )


async def test_suite_enqueue_bounds_a_queued_job_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _OpenFakeRedis()
    _patch_suite_enqueue(monkeypatch, _SuiteQueue(redis, name="runs"), redis)

    job_id = await sw._enqueue_suite_run_execution("suite-run-1", "org-1")

    assert job_id == "saq:job:runs:suite_run:suite-run-1"
    assert await redis.ttl(job_id) == dispatch.SAQ_QUEUED_JOB_TTL


async def test_suite_enqueue_leaves_a_picked_up_job_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _OpenFakeRedis()
    queue = _SuiteQueue(redis, name="runs")
    queue.status = "active"
    _patch_suite_enqueue(monkeypatch, queue, redis)

    job_id = await sw._enqueue_suite_run_execution("suite-run-1", "org-1")

    assert job_id == "saq:job:runs:suite_run:suite-run-1"
    assert await redis.ttl(job_id) == -1
