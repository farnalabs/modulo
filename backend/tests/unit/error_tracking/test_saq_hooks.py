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


def _make_async_session() -> AsyncMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _mark_session(rowcount: int = 1) -> AsyncMock:
    session = _make_async_session()
    result = AsyncMock()
    result.rowcount = rowcount
    session.execute.return_value = result
    return session


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
        ids=["NEW", "QUEUED", "ACTIVE", "ABORTING", "ABORTED", "none", "queued-str", "active-str"],
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
        assert out["error"] == "traceback..."

    def test_resume_failed_marks_run(self) -> None:
        out = saq_hooks._classify(
            "modulo.core.saq_worker.resume_run",
            "failed",
            "traceback...",
            {"run_id": RUN_ID, "org_id": ORG_ID},
        )
        assert out["action"] == "fail_run"
        assert out["error"] == "traceback..."

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
        mark_session = _mark_session(rowcount=1)
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=MagicMock()) as get_run,
            patch("modulo.core.analytics.record_run_facts", new_callable=AsyncMock) as record_facts,
        ):
            factory.side_effect = [
                MagicMock(return_value=mark_session),
                MagicMock(return_value=_make_async_session()),
            ]
            await saq_hooks.after_process(ctx)

        assert mark_session.execute.await_count == 1
        stmt, params = mark_session.execute.await_args.args
        assert "task_failure" in str(stmt)
        assert "NOT IN ('complete', 'cancelled', 'failed')" in str(stmt)
        assert params == {"rid": RUN_ID, "oid": ORG_ID, "detail": "boom"}
        # rowcount == 1 → the compensating analytics fact is recorded.
        get_run.assert_awaited_once()
        record_facts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_execute_truncates_long_error_detail(self) -> None:
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run",
                Status.FAILED,
                "x" * 6000,
                {"run_id": RUN_ID, "org_id": ORG_ID},
            )
        }
        mark_session = _mark_session(rowcount=1)
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=MagicMock()),
            patch("modulo.core.analytics.record_run_facts", new_callable=AsyncMock),
        ):
            factory.side_effect = [
                MagicMock(return_value=mark_session),
                MagicMock(return_value=_make_async_session()),
            ]
            await saq_hooks.after_process(ctx)

        _stmt, params = mark_session.execute.await_args.args
        assert params["detail"] is not None
        assert len(params["detail"]) == 5000

    @pytest.mark.asyncio
    async def test_failed_execute_none_error_writes_null_detail(self) -> None:
        ctx = {
            "job": _job("modulo.core.saq_worker.execute_run", Status.FAILED, None, {"run_id": RUN_ID, "org_id": ORG_ID})
        }
        mark_session = _mark_session(rowcount=1)
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=MagicMock()),
            patch("modulo.core.analytics.record_run_facts", new_callable=AsyncMock),
        ):
            factory.side_effect = [
                MagicMock(return_value=mark_session),
                MagicMock(return_value=_make_async_session()),
            ]
            await saq_hooks.after_process(ctx)

        _stmt, params = mark_session.execute.await_args.args
        assert params["detail"] is None

    @pytest.mark.asyncio
    async def test_failed_execute_non_str_error_coerced(self) -> None:
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run", Status.FAILED, 12345, {"run_id": RUN_ID, "org_id": ORG_ID}
            )
        }
        mark_session = _mark_session(rowcount=1)
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=MagicMock()),
            patch("modulo.core.analytics.record_run_facts", new_callable=AsyncMock),
        ):
            factory.side_effect = [
                MagicMock(return_value=mark_session),
                MagicMock(return_value=_make_async_session()),
            ]
            await saq_hooks.after_process(ctx)

        _stmt, params = mark_session.execute.await_args.args
        assert params["detail"] == "12345"

    @pytest.mark.asyncio
    async def test_failed_execute_sanitizes_secrets_in_error_detail(self) -> None:
        leaked = "OpenAI call failed with sk-abc1234567890; auth Bearer tok12345xyz"
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run", Status.FAILED, leaked, {"run_id": RUN_ID, "org_id": ORG_ID}
            )
        }
        mark_session = _mark_session(rowcount=1)
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=MagicMock()),
            patch("modulo.core.analytics.record_run_facts", new_callable=AsyncMock),
        ):
            factory.side_effect = [
                MagicMock(return_value=mark_session),
                MagicMock(return_value=_make_async_session()),
            ]
            await saq_hooks.after_process(ctx)

        _stmt, params = mark_session.execute.await_args.args
        assert "sk-abc1234567890" not in params["detail"]
        assert "Bearer tok12345xyz" not in params["detail"]
        assert "<redacted>" in params["detail"]

    @pytest.mark.asyncio
    async def test_guard_rejected_rowcount_zero_skips_facts(self) -> None:
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run", Status.FAILED, "boom", {"run_id": RUN_ID, "org_id": ORG_ID}
            )
        }
        mark_session = _mark_session(rowcount=0)
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock),
            patch("modulo.core.analytics.record_run_facts", new_callable=AsyncMock) as record_facts,
        ):
            factory.side_effect = [MagicMock(return_value=mark_session)]
            await saq_hooks.after_process(ctx)

        assert record_facts.await_count == 0
        assert factory.call_count == 1

    @pytest.mark.asyncio
    async def test_facts_failure_is_fail_open_after_mark(self) -> None:
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run", Status.FAILED, "boom", {"run_id": RUN_ID, "org_id": ORG_ID}
            )
        }
        mark_session = _mark_session(rowcount=1)
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=MagicMock()),
            patch(
                "modulo.core.analytics.record_run_facts",
                new_callable=AsyncMock,
                side_effect=RuntimeError("facts boom"),
            ),
        ):
            factory.side_effect = [
                MagicMock(return_value=mark_session),
                MagicMock(return_value=_make_async_session()),
            ]
            # Must NOT raise — the facts write is fail-open and the run is
            # already marked failed in a separate committed session.
            await saq_hooks.after_process(ctx)

        stmt, params = mark_session.execute.await_args.args
        assert "task_failure" in str(stmt)
        assert params["detail"] == "boom"

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


class TestGetEnginePrepPing:
    def test_engine_created_with_pool_pre_ping(self) -> None:
        saved = saq_hooks._ENGINE
        try:
            saq_hooks._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.modulo_db = "postgres"
            mock_engine = MagicMock()
            with (
                patch.object(saq_hooks, "_ENGINE", None),
                patch.object(saq_hooks, "create_async_engine", return_value=mock_engine) as mock_create,
                patch("modulo.settings.get_settings", return_value=settings_mock),
            ):
                saq_hooks._get_engine()
            _, kwargs = mock_create.call_args
            assert kwargs["pool_pre_ping"] is True
            assert kwargs["connect_args"]["statement_cache_size"] == 0
            assert kwargs["connect_args"]["ssl"] is False
        finally:
            saq_hooks._ENGINE = saved
