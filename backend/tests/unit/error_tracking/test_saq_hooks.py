"""Unit tests for modulo.core.error_tracking.saq_hooks (plan F3d).

Covers the PURE ``_classify`` outcome classifier and the ``after_process`` hook's
action execution (run-failed marking, fire-error ingestion, DB-down safety).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from saq import Status

from modulo.core.error_tracking import saq_hooks

RUN_ID = str(uuid.uuid4())
ORG_ID = str(uuid.uuid4())


def _job(function: str, status: Status | str, error: str | None = None, kwargs: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(function=function, status=status, error=error, kwargs=kwargs or {})


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


class TestClassify:
    def test_complete_is_noop(self) -> None:
        out = saq_hooks._classify("modulo.core.saq_worker.execute_run", Status.COMPLETE, None, {"run_id": RUN_ID})
        assert out == {"action": "noop"}

    @pytest.mark.parametrize(
        "status",
        [Status.NEW, Status.QUEUED, Status.ACTIVE, Status.ABORTING, Status.ABORTED, None, "queued", "active"],
    )
    def test_transient_and_swept_statuses_are_noop(self, status: Status | None) -> None:
        out = saq_hooks._classify("modulo.core.saq_worker.execute_run", status, "boom", {"run_id": RUN_ID})
        assert out == {"action": "noop"}

    def test_execute_failed_marks_run(self) -> None:
        out = saq_hooks._classify(
            "modulo.core.saq_worker.execute_run",
            Status.FAILED,
            "traceback...",
            {"run_id": RUN_ID, "org_id": ORG_ID},
        )
        assert out["action"] == "fail_run"
        assert out["run_id"] == RUN_ID
        assert out["org_id"] == ORG_ID

    def test_resume_failed_marks_run(self) -> None:
        out = saq_hooks._classify(
            "modulo.core.saq_worker.resume_run",
            "failed",
            "traceback...",
            {"run_id": RUN_ID, "org_id": ORG_ID},
        )
        assert out["action"] == "fail_run"

    def test_fire_failed_ingests_error(self) -> None:
        out = saq_hooks._classify(
            "modulo.core.saq_worker.fire_cron_trigger",
            Status.FAILED,
            "boom",
            {"trigger_id": "t1", "org_id": ORG_ID},
        )
        assert out["action"] == "ingest_error"
        assert out["function"] == "modulo.core.saq_worker.fire_cron_trigger"
        assert "fire_cron_trigger" in out["message"]
        assert out["error"] == "boom"

    def test_report_failed_ingests_error(self) -> None:
        out = saq_hooks._classify(
            "modulo.core.saq_worker.fire_report_trigger",
            Status.FAILED,
            "delivery boom",
            {"report_id": "r1", "org_id": ORG_ID},
        )
        assert out["action"] == "ingest_error"

    def test_run_job_missing_run_id_ingests_error(self) -> None:
        out = saq_hooks._classify("modulo.core.saq_worker.execute_run", Status.FAILED, "boom", {"org_id": ORG_ID})
        assert out["action"] == "ingest_error"


# ---------------------------------------------------------------------------
# after_process action execution
# ---------------------------------------------------------------------------


class TestAfterProcess:
    @pytest.mark.asyncio
    async def test_failed_execute_marks_run_failed_guarded(self) -> None:
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run", Status.FAILED, "boom", {"run_id": RUN_ID, "org_id": ORG_ID}
            )
        }
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
        ):
            session = AsyncMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            begin_cm = AsyncMock()
            begin_cm.__aenter__ = AsyncMock(return_value=None)
            begin_cm.__aexit__ = AsyncMock(return_value=False)
            session.begin = MagicMock(return_value=begin_cm)
            factory.return_value = MagicMock(return_value=session)
            await saq_hooks.after_process(ctx)

        assert session.execute.await_count == 1
        stmt, params = session.execute.await_args.args
        assert "task_failure" in str(stmt)
        assert "NOT IN ('complete', 'cancelled', 'failed')" in str(stmt)
        assert params == {"rid": RUN_ID, "oid": ORG_ID}

    @pytest.mark.asyncio
    async def test_failed_fire_ingests_error_event(self) -> None:
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.fire_cron_trigger",
                Status.FAILED,
                "boom",
                {"trigger_id": "t1", "org_id": ORG_ID},
            )
        }
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.error_tracking.ErrorIngestionService") as ingestion_cls,
        ):
            session = AsyncMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            begin_cm = AsyncMock()
            begin_cm.__aenter__ = AsyncMock(return_value=None)
            begin_cm.__aexit__ = AsyncMock(return_value=False)
            session.begin = MagicMock(return_value=begin_cm)
            factory.return_value = MagicMock(return_value=session)
            service = AsyncMock()
            ingestion_cls.return_value = service
            await saq_hooks.after_process(ctx)

        service.ingest.assert_awaited_once()
        ingest_args = service.ingest.await_args.args
        assert ingest_args[2]["source"] == "saq"
        assert ingest_args[2]["context_json"]["function"] == "modulo.core.saq_worker.fire_cron_trigger"

    @pytest.mark.asyncio
    async def test_noop_statuses_do_not_touch_db(self) -> None:
        for status in (Status.QUEUED, Status.ACTIVE, Status.ABORTED, Status.COMPLETE):
            ctx = {"job": _job("modulo.core.saq_worker.execute_run", status, None, {"run_id": RUN_ID})}
            with patch.object(saq_hooks, "_open_factory") as factory:
                await saq_hooks.after_process(ctx)
            factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_job_is_noop(self) -> None:
        with patch.object(saq_hooks, "_open_factory") as factory:
            await saq_hooks.after_process({})
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_down_logs_and_leaves_for_reconcile(self) -> None:
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run", Status.FAILED, "boom", {"run_id": RUN_ID, "org_id": ORG_ID}
            )
        }
        with (
            patch.object(saq_hooks, "_open_factory", side_effect=RuntimeError("db down")),
            patch.object(saq_hooks._log, "exception") as log_exc,
        ):
            await saq_hooks.after_process(ctx)
        log_exc.assert_called_once()
        # No exception escapes — the hook must never break the worker.
