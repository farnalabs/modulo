"""Unit tests for modulo.core.error_tracking.saq_hooks (plan F3d).

Covers the PURE ``_classify`` outcome classifier and the ``after_process`` hook's
action execution (run-failed marking, fire-error ingestion, DB-down safety).
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Self
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
        # FAR-583: the guard is the FULL terminal vocabulary + the explicit
        # 'unknown' exclusion (an unknown run is never transitioned here).
        assert "status <> 'unknown'" in str(stmt)
        assert "'complete'" in str(stmt)
        assert "'eval_failed'" in str(stmt)
        assert "'stalled'" in str(stmt)
        assert params == {"rid": RUN_ID, "oid": ORG_ID, "detail": "boom"}
        # rowcount == 1 → the compensating analytics fact is recorded.
        get_run.assert_awaited_once()
        record_facts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_execute_preserves_long_error_detail(self) -> None:
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
        # error_detail was widened String(5000) -> Text by migration 0199, so the
        # write site no longer truncates (FAR-583 path uses limit=None); the full
        # 6000-char sanitized payload is preserved rather than capped at 5000.
        assert len(params["detail"]) == 6000

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
    async def test_failed_fire_without_org_skips_ingest_for_system_sentinel(self) -> None:
        """A failed job with no tenant org resolves to ``SYSTEM_ORG_ID``.

        The system sentinel has no tenant partition to write into, so
        ``_ingest_error_event`` must log the system error and return WITHOUT
        opening a DB session. Covers the ``parsed == SYSTEM_ORG_ID`` arm
        (the sibling ``test_failed_fire_ingests_error_event`` covers a real
        org).
        """
        ctx = {
            "job": _job(
                "modulo.core.saq_worker.fire_cron_trigger",
                Status.FAILED,
                "boom",
                {},  # no org_id -> SYSTEM_ORG_ID sentinel
            )
        }
        with (
            patch.object(saq_hooks, "_open_factory") as factory,
            patch.object(saq_hooks._log, "error") as log_error,
        ):
            await saq_hooks.after_process(ctx)

        factory.assert_not_called()
        assert any("no tenant context" in str(call.args[0]) for call in log_error.call_args_list)

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
    base_url = "postgresql+asyncpg://worker:pw@db:5432/modulo"

    def test_engine_created_with_pool_pre_ping(self) -> None:
        saved = saq_hooks._ENGINE
        try:
            saq_hooks._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.database_url = self.base_url
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

    def test_pool_recycle_from_settings_below_the_proxy_window(self) -> None:
        """FAR-1524: pool_recycle comes from Settings.db_pool_recycle_seconds
        and must stay strictly below the Fly HAProxy 30m session window
        (1800 s) — never the old hardcoded 3600 s."""
        saved = saq_hooks._ENGINE
        try:
            saq_hooks._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.database_url = self.base_url
            settings_mock.db_pool_recycle_seconds = 1500
            mock_engine = MagicMock()
            with (
                patch.object(saq_hooks, "_ENGINE", None),
                patch.object(saq_hooks, "create_async_engine", return_value=mock_engine) as mock_create,
                patch("modulo.settings.get_settings", return_value=settings_mock),
            ):
                saq_hooks._get_engine()
            _, kwargs = mock_create.call_args
            assert kwargs["pool_recycle"] == 1500
            assert kwargs["pool_recycle"] < 1800
            assert kwargs["pool_pre_ping"] is True
        finally:
            saq_hooks._ENGINE = saved

    def test_translates_sslmode_require_onto_connect_args(self) -> None:
        saved = saq_hooks._ENGINE
        try:
            saq_hooks._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.database_url = "postgresql+asyncpg://u:p@h/db?sslmode=require"
            mock_engine = MagicMock()
            with (
                patch.object(saq_hooks, "_ENGINE", None),
                patch.object(saq_hooks, "create_async_engine", return_value=mock_engine) as mock_create,
                patch("modulo.settings.get_settings", return_value=settings_mock),
            ):
                saq_hooks._get_engine()
            kwargs = mock_create.call_args.kwargs
            # sslmode is stripped from the URL and passed as asyncpg's ssl arg;
            # leaving it in the URL raises TypeError at first connect (FAR-1440).
            assert kwargs["url"] == "postgresql+asyncpg://u:p@h/db"
            assert kwargs["connect_args"]["ssl"] == "require"
            assert kwargs["connect_args"]["statement_cache_size"] == 0
        finally:
            saq_hooks._ENGINE = saved

    def test_honours_sslmode_require(self) -> None:
        """FAR-1441: sslmode=require must reach asyncpg as ssl='require'
        (TLS required, fail-closed), never silently downgraded to ssl=False."""
        saved = saq_hooks._ENGINE
        try:
            saq_hooks._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.database_url = self.base_url + "?sslmode=require"
            mock_engine = MagicMock()
            with (
                patch.object(saq_hooks, "_ENGINE", None),
                patch.object(saq_hooks, "create_async_engine", return_value=mock_engine) as mock_create,
                patch("modulo.settings.get_settings", return_value=settings_mock),
            ):
                saq_hooks._get_engine()
            url_arg, kwargs = mock_create.call_args
            assert kwargs["connect_args"]["ssl"] == "require"
            # asyncpg rejects sslmode inside the DSN — it must live only in ssl.
            assert "sslmode" not in str(url_arg)
        finally:
            saq_hooks._ENGINE = saved

    def test_fails_closed_on_downgrading_sslmode(self) -> None:
        """sslmode=prefer/allow refuse the engine loudly — no silent downgrade."""
        saved = saq_hooks._ENGINE
        try:
            saq_hooks._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.database_url = self.base_url + "?sslmode=prefer"
            with (
                patch.object(saq_hooks, "_ENGINE", None),
                patch.object(saq_hooks, "create_async_engine"),
                patch("modulo.settings.get_settings", return_value=settings_mock),
                pytest.raises(ValueError, match="sslmode"),
            ):
                saq_hooks._get_engine()
            assert saq_hooks._ENGINE is None
        finally:
            saq_hooks._ENGINE = saved

    def test_non_postgres_url_gets_no_asyncpg_args(self) -> None:
        """The ssl/timeout/statement_cache_size connect args and pool knobs are
        Postgres-only: a non-Postgres driver URL passes through with none of
        them."""
        saved = saq_hooks._ENGINE
        try:
            saq_hooks._ENGINE = None
            settings_mock = MagicMock()
            settings_mock.database_url = "sqlite+aiosqlite:///tmp/errorhooks.db"
            mock_engine = MagicMock()
            with (
                patch.object(saq_hooks, "_ENGINE", None),
                patch.object(saq_hooks, "create_async_engine", return_value=mock_engine) as mock_create,
                patch("modulo.settings.get_settings", return_value=settings_mock),
            ):
                saq_hooks._get_engine()
            _, kwargs = mock_create.call_args
            assert kwargs == {"url": "sqlite+aiosqlite:///tmp/errorhooks.db"}
        finally:
            saq_hooks._ENGINE = saved


# ---------------------------------------------------------------------------
# FAR-1601 — `_mark_run_failed`'s hot `runs` UPDATE is bounded on EVERY path
# ---------------------------------------------------------------------------
# `_mark_run_failed` runs its guarded `UPDATE runs SET status='failed' ...`
# inside the SAQ worker's after_process hook — a place where an unbounded
# row-lock wait does not just hang one request, it wedges job-outcome
# reconciliation for the whole worker. Until FAR-1601 the bound was applied
# ONLY when a caller passed `lock_timeout_ms` (sole opt-in:
# `run_outputs_dualwrite`, 2000 ms), so the primary caller — `after_process`
# — ran the write with NO bound. These tests pin: (1) the bound is issued by
# default, is `SET LOCAL` (transaction-local), and takes its value from
# `Settings.mutation_row_lock_timeout_ms` — FAIL-FIRST, without FAR-1601 the
# UPDATE is the first statement; (2) a 55P03 expiry is handled NON-SILENTLY —
# a distinct WARNING naming the SQLSTATE and the bound, no exception escaping
# `after_process`, and the run left exactly as any other after_process DB
# failure leaves it (the module's documented "log + leave for
# dispatcher_reconcile" contract).


class _PgRecordingMarkSession:
    """AsyncSession double reporting the postgresql dialect, recording SQL.

    `_mark_run_failed` gates its bound on ``get_bind().dialect.name`` (SQLite
    has no ``SET LOCAL lock_timeout``), so this double takes the LIVE branch
    and records the bound alongside the UPDATE for ORDER/value assertions.
    Plain ``AsyncMock`` doubles report neither dialect, which keeps every
    pre-existing assertion over their ``execute.await_count`` unchanged.
    """

    def __init__(self, rowcount: int = 1) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, object] | None] = []
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._bind = bind
        self._rowcount = rowcount
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        self._begin_cm = begin_cm

    def get_bind(self) -> MagicMock:
        return self._bind

    def begin(self) -> AsyncMock:
        return self._begin_cm

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> MagicMock:
        self.statements.append(str(stmt))
        self.params.append(params)
        result = MagicMock()
        result.rowcount = self._rowcount
        return result


class TestMarkRunFailedLockBoundFAR1601:
    """FAR-1601: the SAQ job-failure write is bounded even with no caller opt-in."""

    @pytest.mark.asyncio
    async def test_lock_bound_is_issued_by_default(self) -> None:
        """THE fail-first pin: ``after_process`` calls ``_mark_run_failed``
        with NO ``lock_timeout_ms`` — until FAR-1601 that path issued no bound
        at all, so the guarded ``runs`` UPDATE could wait unbounded inside the
        worker's after_process hook.

        The bound is the FIRST statement, carries ``SET LOCAL``
        (transaction-local, reverts on COMMIT/ROLLBACK — it can never leak
        onto the pooled connection the hook opens), and its value comes from
        ``Settings.mutation_row_lock_timeout_ms`` — never a literal. The
        explicit ``lock_timeout_ms`` override still wins when a caller passes
        one (``run_outputs_dualwrite`` keeps its 2000 ms).
        """
        session = _PgRecordingMarkSession()
        with (
            patch.object(saq_hooks, "_open_factory", return_value=MagicMock(return_value=session)),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.settings.get_settings", return_value=MagicMock(mutation_row_lock_timeout_ms=4321)),
        ):
            rowcount = await saq_hooks._mark_run_failed(RUN_ID, ORG_ID)

        assert rowcount == 1
        assert session.statements, "the mark issued no statements"
        assert session.statements[0] == "SET LOCAL lock_timeout = 4321", session.statements
        update_at = [i for i, s in enumerate(session.statements) if "UPDATE runs SET status='failed'" in s]
        assert update_at, f"the run-failed UPDATE never ran; statements={session.statements}"
        assert update_at[0] > 0, "the bound must precede the UPDATE"

    @pytest.mark.asyncio
    async def test_explicit_caller_timeout_still_wins(self) -> None:
        """The documented opt-in is unchanged: a caller that passes
        ``lock_timeout_ms`` gets THAT value, not the settings default."""
        session = _PgRecordingMarkSession()
        with (
            patch.object(saq_hooks, "_open_factory", return_value=MagicMock(return_value=session)),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
            patch("modulo.settings.get_settings", return_value=MagicMock(mutation_row_lock_timeout_ms=4321)),
        ):
            await saq_hooks._mark_run_failed(RUN_ID, ORG_ID, lock_timeout_ms=2000)

        assert session.statements[0] == "SET LOCAL lock_timeout = 2000", session.statements

    @pytest.mark.asyncio
    async def test_after_process_logs_a_bounded_lock_timeout_loudly(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """THE 55P03 contract: the bounded wait expired on the run-failed
        UPDATE.

        The transition is NOT written (the transaction rolled back), but the
        condition is never silent: a distinct WARNING carries the SQLSTATE,
        the knob and the recovery, ``after_process`` does not raise (a hook
        that propagates would take the SAQ worker's job-completion path down
        with it), and the run is left exactly as every other after_process DB
        failure leaves it — the module's documented "log + leave for
        dispatcher_reconcile" contract.
        """
        from asyncpg import exceptions as asyncpg_exceptions
        from sqlalchemy.exc import OperationalError

        ctx = {
            "job": _job(
                "modulo.core.saq_worker.execute_run", Status.FAILED, "boom", {"run_id": RUN_ID, "org_id": ORG_ID}
            )
        }
        lock_timeout = OperationalError(
            "UPDATE runs ...",
            {},
            asyncpg_exceptions.LockNotAvailableError("canceling statement due to lock timeout"),
        )
        with patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, side_effect=lock_timeout):
            caplog.set_level(logging.WARNING, logger="modulo.core.error_tracking.saq_hooks")
            await saq_hooks.after_process(ctx)  # must NOT raise

        messages = [r.message for r in caplog.records]
        assert any("saq_hooks.task_failure_lock_timeout" in m for m in messages)
        assert "55P03" in caplog.text
        assert not any("saq_hooks.after_process_reconcile_failed" in m for m in messages)
