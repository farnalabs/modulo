"""FAR-2002 — the queued-job TTL that bounds the self-hosted Redis key space.

The pre-fix behaviour (proven by the first test failing without the change) was
that an enqueued SAQ job hash carried NO TTL at all: ``_enqueue`` stores it with
a plain ``SET`` and SAQ only applies a TTL on FINISH. Under a worker wedge the
job hashes and the queue grew without bound, and the shipped Compose Redis had
no ``maxmemory`` to stop it.

The TTL is status-gated: only a hash stored as ``queued`` gets one. The worker's
dequeue transition to ``active`` is a plain ``SET`` that clears it, so an
in-flight job is never expired.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis as fakeredis
import pytest
from saq.job import Status
from saq.queue.redis import RedisQueue

from modulo.core import dispatch

_QUEUED_JOB_ID = "saq:job:runs:run:abc"


class _FakeRedisWithInfo(fakeredis.FakeRedis):
    """FakeRedis plus the one command ``RedisQueue.version()`` needs.

    fakeredis 2.40 does not implement ``INFO``; ``RedisQueue.dequeue`` calls it
    to choose between ``BRPOPLPUSH`` and ``BLMOVE``, so a real queue on the fake
    needs a version answer. Reporting 7.2 exercises the modern ``BLMOVE`` path.
    """

    async def info(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"redis_version": "7.2.4"}


def _redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


def _job_payload(status: str) -> str:
    # saq serializes with json.dumps (default separators) — the Lua script
    # cjson-decodes it, so the exact spacing does not matter.
    return json.dumps({"function": "modulo.core.saq_worker.execute_run", "status": status})


async def test_queued_ttl_bounds_a_queued_job_hash() -> None:
    redis = _redis()
    await redis.set(_QUEUED_JOB_ID, _job_payload("queued"))

    await dispatch._apply_queued_ttl(redis, _QUEUED_JOB_ID)  # type: ignore[arg-type]

    assert await redis.ttl(_QUEUED_JOB_ID) == dispatch.SAQ_QUEUED_JOB_TTL


async def test_queued_ttl_leaves_an_in_flight_job_alone() -> None:
    """The worker flips the hash to ``active`` on dequeue — never expire it."""
    redis = _redis()
    await redis.set(_QUEUED_JOB_ID, _job_payload("active"))

    await dispatch._apply_queued_ttl(redis, _QUEUED_JOB_ID)  # type: ignore[arg-type]

    assert await redis.ttl(_QUEUED_JOB_ID) == -1


async def test_queued_ttl_is_a_noop_for_an_absent_job() -> None:
    redis = _redis()

    await dispatch._apply_queued_ttl(redis, _QUEUED_JOB_ID)  # type: ignore[arg-type]

    assert await redis.exists(_QUEUED_JOB_ID) == 0


async def test_dequeue_then_active_clears_the_queued_ttl() -> None:
    """Pin the real enqueue -> TTL -> dequeue -> ACTIVE transition (FAR-2002).

    The safety argument rests on SAQ writing the job hash with a plain ``SET``
    when the worker marks it ``active``: ``SET`` clears the TTL. If that ever
    became ``SET ... KEEPTTL``, every in-flight job would inherit the 3600s
    queued TTL and could expire mid-run — a run's timeout is 7200s. This drives
    a REAL ``RedisQueue`` (not a stub) through the exact production sequence.
    """
    redis = _FakeRedisWithInfo()
    queue = RedisQueue(redis, name="runs")

    job = await queue.enqueue(
        "modulo.core.saq_worker.execute_run",
        key="run:abc",
        timeout=dispatch.SAQ_RUN_TIMEOUT,
    )
    assert job is not None
    await dispatch._apply_queued_ttl(redis, job.id)  # type: ignore[arg-type]
    assert await redis.ttl(job.id) == dispatch.SAQ_QUEUED_JOB_TTL

    dequeued = await queue.dequeue(timeout=0.0)
    assert dequeued is not None
    assert dequeued.id == job.id
    # ``dequeue`` only moves the id between the queue lists; the hash — and its
    # queued TTL — is untouched until the worker marks the job active.
    assert await redis.ttl(job.id) == dispatch.SAQ_QUEUED_JOB_TTL

    await dequeued.update(status=Status.ACTIVE)

    assert await redis.ttl(job.id) == -1


async def test_queued_ttl_cancellation_propagates() -> None:
    """A cancelled dispatch must not be swallowed by the best-effort handler."""
    redis = AsyncMock()
    redis.eval.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await dispatch._apply_queued_ttl(redis, _QUEUED_JOB_ID)  # type: ignore[arg-type]


async def test_queued_ttl_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """The job is already enqueued — a TTL failure must not fail the dispatch."""
    redis = AsyncMock()
    redis.eval.side_effect = RuntimeError("EVAL unsupported")

    with caplog.at_level("WARNING"):
        await dispatch._apply_queued_ttl(redis, _QUEUED_JOB_ID)  # type: ignore[arg-type]

    assert "could not apply" in caplog.text


class _FakeQueue:
    """Stand-in for ``saq.queue.redis.RedisQueue`` — no real queue involved."""

    def __init__(self, *, existing: Any = None, enqueued: Any = None) -> None:
        self._existing = existing
        self._enqueued = enqueued

    async def job(self, key: str) -> Any:
        return self._existing

    def job_id(self, key: str) -> str:
        return f"saq:job:runs:{key}"

    async def enqueue(self, *args: Any, **kwargs: Any) -> Any:
        return self._enqueued


def _patch_enqueue(monkeypatch: pytest.MonkeyPatch, queue: _FakeQueue) -> AsyncMock:
    redis = AsyncMock()
    monkeypatch.setattr(dispatch, "_get_shared_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(dispatch, "RedisQueue", lambda *_a, **_k: queue)
    monkeypatch.setattr(
        dispatch,
        "get_settings",
        lambda: SimpleNamespace(saq_job_heartbeat=300, saq_run_retries=5, saq_retry_delay=60),
    )
    return redis


async def test_enqueue_saq_applies_the_ttl_to_the_new_job(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _patch_enqueue(
        monkeypatch,
        _FakeQueue(enqueued=SimpleNamespace(id=_QUEUED_JOB_ID)),
    )
    apply_ttl = AsyncMock()
    monkeypatch.setattr(dispatch, "_apply_queued_ttl", apply_ttl)

    job_id, deduped = await dispatch._enqueue_saq("abc", "org", "runs", "execute_run", None, key_suffix=None)

    assert (job_id, deduped) == (_QUEUED_JOB_ID, False)
    apply_ttl.assert_awaited_once_with(redis, _QUEUED_JOB_ID)


async def test_enqueue_saq_applies_the_ttl_to_a_deduped_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing queued hash (e.g. enqueued before this change) is bounded too."""
    redis = _patch_enqueue(monkeypatch, _FakeQueue(existing=object()))
    apply_ttl = AsyncMock()
    monkeypatch.setattr(dispatch, "_apply_queued_ttl", apply_ttl)

    job_id, deduped = await dispatch._enqueue_saq("abc", "org", "runs", "execute_run", None, key_suffix=None)

    assert (job_id, deduped) == ("saq:job:runs:run:abc", True)
    apply_ttl.assert_awaited_once_with(redis, "saq:job:runs:run:abc")


async def test_enqueue_saq_applies_the_ttl_when_the_script_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``enqueue`` returning None means SAQ's key-based dedupe won — bound it too."""
    redis = _patch_enqueue(monkeypatch, _FakeQueue(existing=None, enqueued=None))
    apply_ttl = AsyncMock()
    monkeypatch.setattr(dispatch, "_apply_queued_ttl", apply_ttl)

    job_id, deduped = await dispatch._enqueue_saq("abc", "org", "runs", "execute_run", None, key_suffix=None)

    assert (job_id, deduped) == ("saq:job:runs:run:abc", True)
    apply_ttl.assert_awaited_once_with(redis, "saq:job:runs:run:abc")
