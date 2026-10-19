"""FAR-2002 - a queued job hash is TTL'd only where a lost hash is recoverable.

The first change applied the status-gated queued TTL only in
``core.dispatch._enqueue_saq``. It was then extended to two more enqueue
paths, which surfaced the rule this module now pins:

* ``core.cron_helpers._enqueue_fire_job_async`` - the per-item trigger fire job.
  Cron/catch-up fires are repairable by ``_fire_missed_cron_epochs`` and
  polling/ongoing fires re-enqueue at the next interval, so the TTL is safe for
  them. Report fires have NO repair (``next_send_at`` is advanced before the
  enqueue, with no rollback and no missed-report scan), so the helper takes
  ``repairable=False`` for them and the hash stays unbounded while queued.
* ``core.saq_worker._enqueue_suite_run_execution`` - nothing reconciles a
  stranded ``pending`` ``SuiteRun``, so its execution hash is deliberately NOT
  TTL'd either.

The TTL stays status-gated: the Lua script only expires a hash whose stored
status is ``queued``, so a job the worker has picked up (``active``) is never
expired.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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


async def test_report_fire_enqueue_is_not_bound() -> None:
    """A report fire has no repair path, so it must not take the queued TTL."""
    redis = _OpenFakeRedis()
    queue = _FireQueue(redis)

    job_id = await ch._enqueue_fire_job_async(
        queue,
        "modulo.core.saq_worker.fire_report_trigger",
        key="epoch-1",
        repairable=False,
    )

    assert job_id == "saq:job:fire:epoch-1"
    assert await redis.ttl(job_id) == -1


def _cron_row(expression: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="trigger-1",
        pipeline_id="pipeline-1",
        cron_expression=expression,
        cron_timezone="UTC",
        run_kind="run",
        next_fire_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("*/15 * * * *", True),  # 15 min - next fire follows within one cadence
        ("0 * * * *", True),  # hourly - inside the catch-up window
        ("0 0 * * 0", False),  # weekly - a lost slot would fall outside the bound
        ("0 0 1 * *", False),  # monthly - same
    ],
)
def test_cron_ttl_is_repairable_only_inside_the_catchup_window(expression: str, expected: bool) -> None:
    assert ch._cron_fire_ttl_is_repairable(_cron_row(expression), datetime.now(UTC)) is expected


async def test_weekly_cron_fire_passes_repairable_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the cron call-site wiring: a long-cadence row opts OUT of the TTL."""
    enqueue = AsyncMock(return_value="job-1")
    monkeypatch.setattr(ch, "_enqueue_fire_job_async", enqueue)
    redis_client = AsyncMock()
    row = _cron_row("0 0 * * 0")
    summary: dict[str, Any] = {"enqueue_failures": 0, "cron_enqueued": 0}

    ok = await ch._enqueue_cron_fire(
        SimpleNamespace(redis=redis_client),  # type: ignore[arg-type]
        redis_client,
        datetime.now(UTC),
        "org-1",
        row,
        None,
        set(),
        summary,
    )

    assert ok is True
    assert enqueue.call_args.kwargs["repairable"] is False


async def test_report_due_row_passes_repairable_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the call-site wiring: the report path is the one that opts OUT."""
    enqueue = AsyncMock(return_value="job-1")
    monkeypatch.setattr(ch, "_enqueue_fire_job_async", enqueue)
    monkeypatch.setattr(ch, "_advance_report_next_send", AsyncMock(return_value=True))
    row = SimpleNamespace(id="report-1", cron_expression="0 * * * *", run_kind="run")
    summary: dict[str, Any] = {"report_due": 0, "report_enqueued": 0, "enqueue_failures": 0}

    await ch._process_one_due_report_row(object(), None, datetime.now(UTC), "org-1", row, summary)  # type: ignore[arg-type]

    assert enqueue.call_args.kwargs["repairable"] is False
    assert summary["report_enqueued"] == 1


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


@pytest.mark.parametrize("status", ["queued", "active"])
async def test_suite_enqueue_never_applies_the_queued_ttl(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    """No stranded-``pending`` ``SuiteRun`` is ever reconciled, so no TTL here.

    The hash keeps whatever SAQ wrote (no expiry) in BOTH the queued and the
    already-picked-up case: the deliberate trade-off is an unbounded queue hash
    rather than a suite run silently lost forever.
    """
    redis = _OpenFakeRedis()
    queue = _SuiteQueue(redis, name="runs")
    queue.status = status
    _patch_suite_enqueue(monkeypatch, queue, redis)

    job_id = await sw._enqueue_suite_run_execution("suite-run-1", "org-1")

    assert job_id == "saq:job:runs:suite_run:suite-run-1"
    assert await redis.ttl(job_id) == -1
