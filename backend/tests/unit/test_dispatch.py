"""Unit tests for modulo.core.dispatch — dispatch_run routing (plan F3e/F6).

Mock/fake based — no Postgres, no Redis. Covers:
  * capacity -> deferred (no enqueue, no dispatched_at)
  * SAQ_ENABLED=false -> Celery route + dispatcher NULL
  * SAQ_ENABLED=true  -> SAQ route + dispatcher 'saq' + enqueued
  * enqueued vs deduped
  * enqueue failure -> dispatch_failed + webhook dedup expiry (non-webhook)
  * fail-fast (webhook) enqueue failure -> deferred, no block
  * claim_token distinct from saq_job_id
  * error enum: 'saq' accepted by the validator, unknown rejected
  * the 7+1 call sites route through dispatch_run / dispatch_run_sync
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import modulo.core.dispatch as dispatch
from modulo.api.models.error import ErrorEventInput
from modulo.core.cron_scheduler import CronFireTask
from modulo.core.trigger_engine.polling import PollingFireTask

RUN_ID = "fb4b1368-68ca-4125-8091-ca8d7c25839e"
ORG_ID = "18348064-eca3-4aa7-be96-8f6c9123efd0"
JOB_ID = f"saq:job:runs:run:{RUN_ID}"


class _MockBegin:
    def __init__(self) -> None:
        self.entered = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Async session double supporting ``async with session.begin():``."""

    def __init__(self) -> None:
        self.begin_cm = _MockBegin()

    def begin(self) -> _MockBegin:
        return self.begin_cm

    async def close(self) -> None:
        return None


def _make_settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_enabled": False,
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_job_heartbeat": 300,
        "saq_run_retries": 5,
        "saq_retry_delay": 60,
        "saq_redis_pool_size": 5,
    }
    base.update(overrides)
    return MagicMock(**base)


def _rls_patch() -> MagicMock:
    """Patch set_rls_org (imported lazily inside dispatch_run)."""
    return patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock)


def _enqueue_patch(**kwargs: object) -> MagicMock:
    return patch.object(
        dispatch,
        "_enqueue_saq",
        new_callable=AsyncMock,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# dispatch_run routing
# ---------------------------------------------------------------------------


class TestDispatchRunRouting:
    @pytest.mark.asyncio
    async def test_capacity_deferred_no_enqueue_no_dispatched_at(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings(saq_enabled=True)),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=True),
            _enqueue_patch() as enqueue,
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock) as dispatched,
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "deferred"
        assert job_id is None
        enqueue.assert_not_called()
        dispatched.assert_not_called()

    @pytest.mark.asyncio
    async def test_shadow_execute_routes_to_celery_dispatcher_null(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings(saq_enabled=False)),
            _rls_patch(),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock) as dispatched,
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock) as saq_job,
            patch("modulo.core.pipeline_executor_task.dispatch") as celery_dispatch,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID, celery_queue="runs_automated")

        assert outcome == "enqueued"
        assert job_id is None
        celery_dispatch.assert_called_once_with(RUN_ID, ORG_ID, "runs_automated")
        dispatched.assert_called_once()
        # dispatcher stays NULL — the SAQ job write is never issued
        saq_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_saq_enabled_execute_enqueues_and_sets_dispatcher(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings(saq_enabled=True)),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(return_value=(JOB_ID, False)),
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock) as saq_job,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID, queue="runs")

        assert outcome == "enqueued"
        assert job_id == JOB_ID
        saq_job.assert_awaited_once()
        args = saq_job.await_args.args
        assert args[2] == JOB_ID
        assert args[3] != JOB_ID

    @pytest.mark.asyncio
    async def test_shadow_resume_routes_to_saq_dispatcher_saq(self) -> None:
        # F6a carve-out: resume_run routes to SAQ even in shadow.
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings(saq_enabled=False)),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(return_value=(JOB_ID, False)),
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock) as saq_job,
        ):
            outcome, job_id = await dispatch.dispatch_run(
                RUN_ID, ORG_ID, job_type="resume_run", resume_data={"action": "approved"}
            )

        assert outcome == "enqueued"
        assert job_id == JOB_ID
        saq_job.assert_awaited_once()
        args = saq_job.await_args.args
        assert args[2] == JOB_ID

    @pytest.mark.asyncio
    async def test_deduped_returns_deduped(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings(saq_enabled=True)),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(return_value=(JOB_ID, True)),
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock),
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "deduped"
        assert job_id == JOB_ID

    @pytest.mark.asyncio
    async def test_enqueue_failure_marks_dispatch_failed_and_expires_dedup(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings(saq_enabled=True)),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=RuntimeError("redis down")),
            patch.object(dispatch.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(dispatch, "_mark_dispatch_failed", new_callable=AsyncMock) as mark_failed,
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock) as expire_dedup,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "deferred"
        assert job_id is None
        mark_failed.assert_awaited()
        expire_dedup.assert_awaited()

    @pytest.mark.asyncio
    async def test_enqueue_failure_fail_fast_does_not_block(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings(saq_enabled=True)),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=RuntimeError("redis down")),
            patch.object(dispatch, "_mark_dispatch_failed", new_callable=AsyncMock) as mark_failed,
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock) as expire_dedup,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID, fail_fast=True)

        assert outcome == "deferred"
        assert job_id is None
        mark_failed.assert_not_called()
        expire_dedup.assert_not_called()


class TestClaimToken:
    def test_token_distinct_from_saq_job_id(self) -> None:
        token = dispatch._new_claim_token()
        job_id = f"saq:job:runs:run:{uuid.uuid4()}"
        assert token
        assert token != job_id
        assert token.isalnum()


# ---------------------------------------------------------------------------
# Error enum
# ---------------------------------------------------------------------------


class TestErrorSourceEnum:
    def test_saq_accepted_by_validator(self) -> None:
        ev = ErrorEventInput(level="error", message="boom", source="saq")
        assert ev.source == "saq"

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ErrorEventInput(level="error", message="boom", source="unknown")

    def test_constraint_contains_saq(self) -> None:
        from sqlalchemy import CheckConstraint

        from modulo.db.models import Base

        table = Base.metadata.tables["error_events"]
        check = next(
            c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_error_events_source"
        )
        sql = str(check.sqltext)
        assert "'saq'" in sql
        assert "'celery'" in sql


# ---------------------------------------------------------------------------
# SAQ enqueue knobs
# ---------------------------------------------------------------------------


class TestEnqueueSaq:
    @pytest.mark.asyncio
    async def test_enqueue_uses_key_and_knobs(self) -> None:
        enqueue_mock = AsyncMock(return_value=SimpleNamespace(id=JOB_ID))
        queue_cls = MagicMock()
        queue_instance = queue_cls.return_value
        queue_instance.enqueue = enqueue_mock
        redis_cls = MagicMock()
        redis_client = redis_cls.from_url.return_value
        redis_client.aclose = AsyncMock(return_value=None)

        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            patch.object(dispatch, "RedisQueue", queue_cls),
            patch.object(dispatch, "AsyncRedis", redis_cls),
        ):
            job_id, deduped = await dispatch._enqueue_saq(RUN_ID, ORG_ID, "runs", "execute_run", None)

        assert job_id == JOB_ID
        assert deduped is False
        call_args = enqueue_mock.await_args
        assert call_args.args[0] == dispatch.SAQ_EXECUTE_RUN_FUNCTION
        call_kwargs = call_args.kwargs
        assert call_kwargs["key"] == f"run:{RUN_ID}"
        assert call_kwargs["timeout"] == 7200
        assert call_kwargs["ttl"] == 300
        assert call_kwargs["retries"] == 5
        assert call_kwargs["retry_delay"] == 60
        assert call_kwargs["retry_backoff"] is False
        assert call_kwargs["run_id"] == RUN_ID
        assert call_kwargs["org_id"] == ORG_ID

    @pytest.mark.asyncio
    async def test_resume_run_passes_resume_data_and_function(self) -> None:
        enqueue_mock = AsyncMock(return_value=SimpleNamespace(id=JOB_ID))
        queue_cls = MagicMock()
        queue_instance = queue_cls.return_value
        queue_instance.enqueue = enqueue_mock
        redis_cls = MagicMock()
        redis_client = redis_cls.from_url.return_value
        redis_client.aclose = AsyncMock(return_value=None)

        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            patch.object(dispatch, "RedisQueue", queue_cls),
            patch.object(dispatch, "AsyncRedis", redis_cls),
        ):
            await dispatch._enqueue_saq(RUN_ID, ORG_ID, "runs", "resume_run", {"action": "approved", "notes": "ok"})

        call_args = enqueue_mock.await_args
        assert call_args.args[0] == dispatch.SAQ_RESUME_RUN_FUNCTION
        call_kwargs = call_args.kwargs
        assert call_kwargs["resume_data"] == {"action": "approved", "notes": "ok"}


# ---------------------------------------------------------------------------
# Call-site conversions (light mock-based per site)
# ---------------------------------------------------------------------------


class TestCallSiteConversions:
    def test_webhooks_module_uses_dispatch_run_sync(self) -> None:
        import modulo.api.routes.webhooks as webhooks

        assert webhooks.dispatch_run_sync is dispatch.dispatch_run_sync

    def test_runs_manual_route_uses_dispatch_run(self) -> None:
        import modulo.api.routes.runs as runs

        assert runs.dispatch_run is dispatch.dispatch_run

    def test_mcp_server_uses_dispatch_run(self) -> None:
        import modulo.api.mcp_server as mcp

        assert mcp.dispatch_run is dispatch.dispatch_run

    def test_scheduler_uses_dispatch_run(self) -> None:
        import modulo.core.scheduler as scheduler

        assert scheduler.dispatch_run is dispatch.dispatch_run

    def test_cron_scheduler_uses_dispatch_run_sync(self) -> None:
        import modulo.core.cron_scheduler as cron

        assert cron.dispatch_run_sync is dispatch.dispatch_run_sync

    def test_polling_uses_dispatch_run_sync(self) -> None:
        import modulo.core.trigger_engine.polling as polling

        assert polling.dispatch_run_sync is dispatch.dispatch_run_sync


class TestCronFireTaskDispatch:
    def test_cron_fire_dispatches_via_dispatch_run_sync(self) -> None:
        task = CronFireTask()
        result = {"status": "fired", "run_id": RUN_ID}
        trigger_id = str(uuid.uuid4())
        pipeline_id = str(uuid.uuid4())
        with (
            patch(
                "modulo.core.cron_scheduler.fire_cron_trigger",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch("modulo.core.cron_scheduler.dispatch_run_sync") as dispatch_sync,
        ):
            out = task.run(trigger_id, ORG_ID, pipeline_id, "*/5 * * * *")

        assert out["status"] == "fired"
        dispatch_sync.assert_called_once_with(result["run_id"], ORG_ID, queue="runs", celery_queue="runs_automated")


class TestPollingFireTaskDispatch:
    def test_polling_fire_dispatches_via_dispatch_run_sync(self) -> None:
        task = PollingFireTask()
        result = {"status": "fired", "run_id": RUN_ID}
        trigger_id = str(uuid.uuid4())
        pipeline_id = str(uuid.uuid4())
        connector_id = str(uuid.uuid4())
        with (
            patch(
                "modulo.core.trigger_engine.polling.fire_polling_trigger",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch("modulo.core.trigger_engine.polling.dispatch_run_sync") as dispatch_sync,
        ):
            out = task.run(
                trigger_id,
                ORG_ID,
                pipeline_id,
                connector_id,
                "SELECT 1",
                "condition == true",
            )

        assert out["status"] == "fired"
        dispatch_sync.assert_called_once_with(result["run_id"], ORG_ID, queue="runs", celery_queue="runs_automated")
