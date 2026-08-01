"""Unit tests for dispatcher_reconcile (plan F3c) — predicate matrix, partial
eviction, Redis-error fail-safe, re-enqueue gate-on-return, discriminator.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch

ORG = uuid.uuid4()
RUN_PENDING_UNDISPATCHED = uuid.uuid4()
RUN_PENDING_DISPATCHED = uuid.uuid4()
RUN_RUNNING = uuid.uuid4()
RUN_AWAITING = uuid.uuid4()
RUN_WITH_JOB = uuid.uuid4()
RUN_EVICTED = uuid.uuid4()


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[tuple[Any, Any]] = []
        self.begin_cm = _MockBegin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return self.begin_cm

    def get_bind(self) -> Any:
        return self._get_bind()

    async def get(self, model: Any, pk: Any) -> SimpleNamespace:
        return SimpleNamespace(max_concurrent_runs=5)

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        if "set_config" in str(stmt):
            return MagicMock()
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _run_row(run_id: uuid.UUID, status: str, *, dispatched: bool = True, stale: bool = True) -> SimpleNamespace:
    heartbeat = datetime.now(UTC) - timedelta(minutes=30) if stale else datetime.now(UTC)
    return SimpleNamespace(
        id=run_id,
        pipeline_id=uuid.uuid4(),
        status=status,
        dispatched_at=datetime.now(UTC) if dispatched else None,
        heartbeat_at=heartbeat,
    )


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_enabled": True,
        "saq_runs_queue": "runs",
        "saq_reenqueue_window": 600,
        "saq_job_heartbeat": 300,
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
    }
    base.update(overrides)
    return MagicMock(**base)


def _make_queue(redis_client: MagicMock, *, job_result: Any = None) -> MagicMock:
    q = MagicMock()
    q.name = "runs"
    q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
    q.job = AsyncMock(return_value=job_result)
    return q


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


async def _run_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[Any],
    *,
    queue_job_result: Any = None,
    dispatch_result: tuple[str, str | None] = ("enqueued", "new-job-id"),
    capacity_free: bool = True,
) -> dict[str, Any]:
    _patch_env(monkeypatch)
    session = _MockSession([_org_result([ORG]), _rows_result(rows)])
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    q = _make_queue(redis_client, job_result=queue_job_result)
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client

    with (
        patch.object(ch, "_open_factory", return_value=factory),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis", redis_cls),
        patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
        patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=dispatch_result) as reenqueue,
        patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
    ):
        if capacity_free is False:
            with patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=5):
                summary = await ch.dispatcher_reconcile()
        else:
            with patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0):
                summary = await ch.dispatcher_reconcile()

    return summary, reenqueue, ingest, redis_client


class TestReconcilePredicateMatrix:
    @pytest.mark.asyncio
    async def test_pending_undispatched_capacity_free_redispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, ingest, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)]
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_undispatched_capacity_full_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _ingest, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)], capacity_free=False
        )
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        assert summary["skipped"] == 1

    @pytest.mark.asyncio
    async def test_pending_dispatched_stale_redispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_DISPATCHED, "pending", dispatched=True, stale=True)]
        )
        assert summary["repaired"] == 1
        assert reenqueue.await_args.args[3] == "execute_run"

    @pytest.mark.asyncio
    async def test_running_stale_redispatch_execute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _, _ = await _run_reconcile(monkeypatch, [_run_row(RUN_RUNNING, "running", stale=True)])
        assert summary["repaired"] == 1
        assert reenqueue.await_args.args[3] == "execute_run"

    @pytest.mark.asyncio
    async def test_awaiting_human_never_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F6a review: a waiting HITL run must NEVER be re-dispatched — its
        execute_run job COMPLETED normally at the gate, and re-dispatching as
        resume_run with an empty decision would auto-approve the gate."""
        summary, reenqueue, ingest, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "awaiting_human", stale=True)]
        )
        assert summary["repaired"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claimed_never_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _, _ = await _run_reconcile(monkeypatch, [_run_row(RUN_AWAITING, "claimed", stale=True)])
        assert summary["repaired"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_capacity_deferred_redispatched_in_saq_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Capacity-deferred runs (pending, dispatched_at NULL, dispatcher NULL)
        must be reachable and re-dispatched when capacity frees (F3c)."""
        summary, reenqueue, ingest, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)],
            capacity_free=True,
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_undispatched_not_redispatched_in_shadow_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In shadow mode a pending+undispatched run is a not-yet-sent Celery
        dispatch, not a SAQ capacity deferral — reconcile must not touch it."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings(saq_enabled=False)),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 0
        assert summary["scanned"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_job_still_exists_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, ingest, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_WITH_JOB, "running", stale=True)],
            queue_job_result=SimpleNamespace(id="saq:job:runs:run:x"),
        )
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()


class TestReconcileRedisFailSafe:
    @pytest.mark.asyncio
    async def test_redis_read_error_does_nothing_and_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_RUNNING, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        q.job = AsyncMock(side_effect=RuntimeError("redis read failed"))
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["redis_errors"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_awaited_once()
        # Fail-safe: NEVER act on an unreadable Redis — no DEL/ZREM/LREM issued.
        redis_client.delete.assert_not_called()
        redis_client.zrem.assert_not_called()
        redis_client.lrem.assert_not_called()


class TestPartialEviction:
    @pytest.mark.asyncio
    async def test_evicted_job_repaired_with_prefix_aware_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worker stopped, job hash deleted -> reconcile DEL+ZREM+LREM+enqueue."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client, job_result=None)  # queue.job returns None
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(
                ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "new-job")
            ) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 1
        job_key = f"run:{RUN_EVICTED}"
        job_id = f"saq:job:runs:run:{RUN_EVICTED}"
        # All keys derived from the configured queue name (PREFIX-AWARE).
        redis_client.delete.assert_awaited_with(f"saq:abort:{job_key}")
        redis_client.zrem.assert_awaited_with("saq:runs:incomplete", job_id)
        redis_client.lrem.assert_any_await("saq:runs:queued", 0, job_id)
        redis_client.lrem.assert_any_await("saq:runs:active", 0, job_id)
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[0] == "runs"
        assert reenqueue.await_args.args[1] == str(RUN_EVICTED)
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_deduped_after_repair_alerts_no_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enqueue gate-on-return: a still-deduped result must not loop."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client, job_result=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("deduped", None)) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 0
        assert summary["deduped"] == 1
        reenqueue.assert_awaited_once()  # gate-on-return: exactly one attempt
        ingest.assert_awaited_once()


class TestReconcilePrefixAware:
    @pytest.mark.asyncio
    async def test_staging_queue_names_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same reconcile against staging-runs must derive staging keys."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_RUNNING, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = MagicMock()
        q.name = "staging-runs"
        q.job_id.side_effect = lambda key: f"saq:job:staging-runs:{key}"
        q.job = AsyncMock(return_value=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings(saq_runs_queue="staging-runs")),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "job")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
        ):
            await ch.dispatcher_reconcile()

        redis_client.zrem.assert_awaited_with("saq:staging-runs:incomplete", f"saq:job:staging-runs:run:{RUN_RUNNING}")
        redis_client.lrem.assert_any_await("saq:staging-runs:queued", 0, f"saq:job:staging-runs:run:{RUN_RUNNING}")
        redis_client.lrem.assert_any_await("saq:staging-runs:active", 0, f"saq:job:staging-runs:run:{RUN_RUNNING}")
