"""Unit tests for modulo.core.dispatch — dispatch_run routing (plan F3e/F6).

Mock/fake based — no Postgres, no Redis. Covers:
  * capacity -> deferred (no enqueue, no dispatched_at)
  * SAQ route + dispatcher 'saq' + enqueued (PR C: SAQ is the only path)
  * enqueued vs deduped (incl. the B2 TOCTOU re-check inside _enqueue_saq)
  * enqueue failure -> NON-terminal 'enqueue_failed' marker + webhook dedup
    expiry (B3 durable dispatch — the run stays pending for reconcile)
  * fail-fast (webhook) enqueue failure -> 'enqueue_failed', no block
  * claim_token distinct from saq_job_id
  * error enum: 'saq' accepted by the validator, unknown rejected
  * the call sites route through dispatch_run (on-loop via BackgroundTasks)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from modulo.api.models.error import ErrorEventInput
from modulo.core import dispatch

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
        # dispatch_run's terminal-status guard reads the run via get_run; a
        # plain run row that is NOT terminal must not block dispatch.
        self._run = SimpleNamespace(status="pending")

    def begin(self) -> _MockBegin:
        return self.begin_cm

    def get_bind(self) -> SimpleNamespace:
        """Dialect gate input for ``set_mutation_row_lock_timeout`` (FAR-1584).

        The dispatch writers call ``db.crud.row_lock.set_mutation_row_lock_timeout``,
        whose shared ``get_dialect_name`` reads ``session.get_bind()``. A
        non-postgres double takes the helper's documented safe no-op branch
        (SQLite/MySQL take no ``lock_timeout``), exactly like the SQLite unit
        fixtures — tests that need the LIVE bound use ``_PgRecordingSession``.
        """
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def close(self) -> None:
        return None

    async def execute(self, *args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._run
        return result


def _make_settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_job_heartbeat": 300,
        "saq_reenqueue_window": 600,
        "saq_run_retries": 5,
        "saq_retry_delay": 60,
        "saq_redis_pool_size": 50,
        "saq_run_claim_cap": 20,
    }
    base.update(overrides)
    return MagicMock(**base)


def _rls_patch() -> MagicMock:
    """Patch set_rls_org / set_rls_execution_context (imported lazily inside dispatch_run)."""
    return patch.multiple(
        "modulo.db.rls",
        set_rls_org=AsyncMock(),
        set_rls_execution_context=AsyncMock(),
    )


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
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=True),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
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
    async def test_terminal_run_never_dispatched(self) -> None:
        """A terminal (eval_failed) run is refused at dispatch — no enqueue."""
        session = _MockSession()
        session._run = SimpleNamespace(status="eval_failed")
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock) as cap,
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock),
            _enqueue_patch() as enqueue,
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock) as dispatched,
            patch.object(dispatch, "_open_session", return_value=session),
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "terminal_skipped"
        assert job_id is None
        enqueue.assert_not_called()
        dispatched.assert_not_called()
        # Capacity gates are never consulted for a terminal run.
        cap.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_enqueues_and_sets_dispatcher(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
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
    async def test_resume_enqueues_and_sets_dispatcher(self) -> None:
        # resume_run enqueues to SAQ (worker wiring, F6a).
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
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

    @pytest.mark.asyncio
    async def test_deduped_returns_deduped(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(return_value=(JOB_ID, True)),
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock),
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "deduped"
        assert job_id == JOB_ID

    @pytest.mark.asyncio
    async def test_enqueue_failure_marks_enqueue_failed_and_expires_dedup(self) -> None:
        """Final enqueue failure (non-webhook): the run is LEFT pending with the
        non-terminal ``enqueue_failed_at`` marker — never terminal-failed — and
        the outcome is DISTINCT ``enqueue_failed`` (not ``deferred``) so the
        caller can record an error_event without confusing it with capacity."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=RuntimeError("redis down")),
            patch.object(dispatch.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(dispatch, "_mark_enqueue_failed", new_callable=AsyncMock) as mark_failed,
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock) as expire_dedup,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "enqueue_failed"
        assert job_id is None
        mark_failed.assert_awaited()
        expire_dedup.assert_awaited()

    @pytest.mark.asyncio
    async def test_enqueue_failure_fail_fast_returns_enqueue_failed(self) -> None:
        """Fail-fast (webhook) enqueue failure -> 'enqueue_failed', no block.

        The run is still LEFT pending with the non-terminal enqueue_failed marker
        and the webhook dedup is expired even though the run awaits
        dispatcher_reconcile recovery — a retried webhook must not be
        suppressed while the run is pending re-dispatch.
        """
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=RuntimeError("redis down")),
            patch.object(dispatch, "_mark_enqueue_failed", new_callable=AsyncMock) as mark_failed,
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock) as expire_dedup,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID, fail_fast=True)

        assert outcome == "enqueue_failed"
        assert job_id is None
        mark_failed.assert_awaited()
        expire_dedup.assert_awaited()

    @pytest.mark.asyncio
    async def test_enqueue_failure_retries_then_enqueues(self) -> None:
        """A transient enqueue failure is retried (1s/2s/3s) before giving up.

        The retry that succeeds must still record the SAQ job and report
        ``enqueued`` -- never ``deferred`` or ``enqueue_failed``.
        """
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=[RuntimeError("transient"), (JOB_ID, False)]),
            patch.object(dispatch.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock) as saq_job,
            patch.object(dispatch, "_mark_enqueue_failed", new_callable=AsyncMock) as mark_failed,
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock) as expire_dedup,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "enqueued"
        assert job_id == JOB_ID
        saq_job.assert_awaited_once()
        mark_failed.assert_not_called()
        expire_dedup.assert_not_called()

    @pytest.mark.asyncio
    async def test_enqueue_cancelled_error_reraises_no_retry(self) -> None:
        """Cancellation during the FIRST enqueue must propagate, not retry.

        Swallowing cancellation into the 1s/2s/3s retry loop would delay an
        abort (shutdown) by ~6s while pretending the run is still dispatchable.
        """
        with (
            pytest.raises(asyncio.CancelledError),
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=asyncio.CancelledError()),
            patch.object(dispatch.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(dispatch, "_mark_enqueue_failed", new_callable=AsyncMock) as mark_failed,
        ):
            await dispatch.dispatch_run(RUN_ID, ORG_ID)

        mark_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_enqueue_retry_cancelled_error_reraises(self) -> None:
        """Cancellation DURING a retry must also propagate immediately.

        After a transient failure, a cancelled retry aborts the loop rather
        than falling through to the ``enqueue_failed`` marker path.
        """
        with (
            pytest.raises(asyncio.CancelledError),
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=[RuntimeError("transient"), asyncio.CancelledError()]),
            patch.object(dispatch.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(dispatch, "_mark_enqueue_failed", new_callable=AsyncMock) as mark_failed,
        ):
            await dispatch.dispatch_run(RUN_ID, ORG_ID)

        mark_failed.assert_not_called()


# ---------------------------------------------------------------------------
# count_active_runs_for_org — include_pending flag + org scoping
# ---------------------------------------------------------------------------


_COUNTABLE_ORG_STATUSES = {"pending", "running", "awaiting_human", "claimed"}


class TestCountActiveRunsForOrg:
    def _in_clause_statuses(self, stmt: object) -> set[str]:
        """Extract the statuses bound into the count query's IN clause."""
        statuses: set[str] = set()
        for value in stmt.compile(dialect=postgresql.dialect()).params.values():
            if isinstance(value, (list, tuple)):
                statuses.update(v for v in value if v in _COUNTABLE_ORG_STATUSES)
            elif value in _COUNTABLE_ORG_STATUSES:
                statuses.add(value)
        return statuses

    async def test_include_pending_false_excludes_pending(self) -> None:
        from modulo.db.crud.run import count_active_runs_for_org

        executed: list[tuple[object, object]] = []

        class _Result:
            def scalar_one_or_none(self) -> int:
                return 0

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _Result:
                executed.append((stmt, stmt))
                return _Result()

        session = _FakeAsyncSession()
        await count_active_runs_for_org(session, uuid.uuid4(), include_pending=False)
        statuses = self._in_clause_statuses(executed[0][1])
        assert statuses == {"running", "awaiting_human", "claimed"}

    async def test_include_pending_true_includes_pending(self) -> None:
        from modulo.db.crud.run import count_active_runs_for_org

        executed: list[object] = []

        class _Result:
            def scalar_one_or_none(self) -> int:
                return 0

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _Result:
                executed.append(stmt)
                return _Result()

        session = _FakeAsyncSession()
        await count_active_runs_for_org(session, uuid.uuid4(), include_pending=True)
        statuses = self._in_clause_statuses(executed[0])
        assert statuses == _COUNTABLE_ORG_STATUSES

    async def test_scoped_to_org_and_excludes_run_id(self) -> None:
        stmt_sql: list[str] = []

        class _Result:
            def scalar_one_or_none(self) -> int:
                return 0

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _Result:
                stmt_sql.append(str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
                return _Result()

        from modulo.db.crud.run import count_active_runs_for_org

        org_id = uuid.uuid4()
        rid = uuid.uuid4()
        session = _FakeAsyncSession()
        await count_active_runs_for_org(session, org_id, include_pending=False, exclude_run_id=rid)
        rendered = stmt_sql[0]
        assert "runs.organisation_id =" in rendered or "organisation_id =" in rendered
        assert "runs.id !=" in rendered or "id !=" in rendered

    async def test_returns_count(self) -> None:
        from modulo.db.crud.run import count_active_runs_for_org

        class _Result:
            def scalar_one_or_none(self) -> int:
                return 4

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _Result:
                return _Result()

        session = _FakeAsyncSession()
        assert await count_active_runs_for_org(session, uuid.uuid4(), include_pending=False) == 4


# ---------------------------------------------------------------------------
# get_org_run_concurrency_limit — fail-open setting reader
# ---------------------------------------------------------------------------


def _org_with_run_settings(settings: Any) -> MagicMock:
    org = MagicMock()
    org.settings_json = settings
    return org


class TestGetOrgRunConcurrencyLimit:
    async def test_unset_returns_none(self) -> None:
        from modulo.db.crud.run import get_org_run_concurrency_limit

        with patch("modulo.db.crud.run.get_organisation", return_value=_org_with_run_settings({})):
            assert await get_org_run_concurrency_limit(AsyncMock(), uuid.uuid4()) is None

    async def test_returns_int(self) -> None:
        from modulo.db.crud.run import get_org_run_concurrency_limit

        org = _org_with_run_settings({"run_concurrency_limit": 5})
        with patch("modulo.db.crud.run.get_organisation", return_value=org):
            assert await get_org_run_concurrency_limit(AsyncMock(), uuid.uuid4()) == 5

    async def test_clamps_out_of_range(self) -> None:
        from modulo.db.crud.run import get_org_run_concurrency_limit

        org_high = _org_with_run_settings({"run_concurrency_limit": 9999})
        with patch("modulo.db.crud.run.get_organisation", return_value=org_high):
            assert await get_org_run_concurrency_limit(AsyncMock(), uuid.uuid4()) == 100
        org_low = _org_with_run_settings({"run_concurrency_limit": 0})
        with patch("modulo.db.crud.run.get_organisation", return_value=org_low):
            assert await get_org_run_concurrency_limit(AsyncMock(), uuid.uuid4()) == 1

    @pytest.mark.parametrize(
        "bad_value",
        ["3", 3.0, True, False, [3], {"v": 3}],
    )
    async def test_fail_open_on_bad_type(self, bad_value: object) -> None:
        from modulo.db.crud.run import get_org_run_concurrency_limit

        with patch(
            "modulo.db.crud.run.get_organisation",
            return_value=_org_with_run_settings({"run_concurrency_limit": bad_value}),
        ):
            assert await get_org_run_concurrency_limit(AsyncMock(), uuid.uuid4()) is None

    async def test_fail_open_on_non_dict_settings(self) -> None:
        from modulo.db.crud.run import get_org_run_concurrency_limit

        with patch("modulo.db.crud.run.get_organisation", return_value=_org_with_run_settings("not-a-dict")):
            assert await get_org_run_concurrency_limit(AsyncMock(), uuid.uuid4()) is None

    async def test_fail_open_on_missing_org(self) -> None:
        from modulo.db.crud.run import get_org_run_concurrency_limit

        with patch("modulo.db.crud.run.get_organisation", return_value=None):
            assert await get_org_run_concurrency_limit(AsyncMock(), uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# _org_capacity_deferred — org run-concurrency admission at dispatch time
# ---------------------------------------------------------------------------


def _run_with_status(status: str) -> MagicMock:
    run = MagicMock()
    run.status = status
    return run


class TestOrgCapacityDeferred:
    @pytest.mark.asyncio
    async def test_defers_and_writes_marker_when_at_cap_and_pending(self) -> None:
        from modulo.db.crud.run import ERROR_CODE_ORG_CAPACITY_LIMITED

        session = AsyncMock()
        run_id = uuid.UUID(RUN_ID)
        org_id = uuid.UUID(ORG_ID)
        with (
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "modulo.db.crud.run.count_active_runs_for_org",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "modulo.db.crud.run.get_run",
                new_callable=AsyncMock,
                return_value=_run_with_status("pending"),
            ),
            patch(
                "modulo.db.crud.run.update_run_status",
                new_callable=AsyncMock,
            ) as update_status,
        ):
            deferred = await dispatch._org_capacity_deferred(session, run_id, org_id)

        assert deferred is True
        update_status.assert_awaited_once_with(
            session,
            run_id,
            "pending",
            error_code=ERROR_CODE_ORG_CAPACITY_LIMITED,
        )

    @pytest.mark.parametrize("current_status", ["running", "awaiting_human", "claimed"])
    @pytest.mark.asyncio
    async def test_defers_non_pending_without_demoting_status(self, current_status: str) -> None:
        """A resume/recovery run at the org cap is deferred WITHOUT demotion.

        ``recover_node`` and ``dispatcher_reconcile`` re-dispatch non-pending
        runs (running/awaiting_human/claimed) as ``resume_run``. Demoting those
        to ``pending`` would drop the resume payload / committed HITL decision,
        so the gate must defer without writing status — mirroring
        ``_capacity_deferred``.
        """
        session = AsyncMock()
        run_id = uuid.UUID(RUN_ID)
        org_id = uuid.UUID(ORG_ID)
        with (
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "modulo.db.crud.run.count_active_runs_for_org",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "modulo.db.crud.run.get_run",
                new_callable=AsyncMock,
                return_value=_run_with_status(current_status),
            ),
            patch("modulo.db.crud.run.update_run_status", new_callable=AsyncMock) as update_status,
        ):
            deferred = await dispatch._org_capacity_deferred(session, run_id, org_id)

        assert deferred is True
        update_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_job_never_org_capacity_deferred(self) -> None:
        """Major 2: a resume_run dispatch bypasses the org-cap gate entirely.

        A resume is the continuation of an ALREADY-ADMITTED run (already
        ``running`` and already consuming an org slot); the org-cap gate only
        applies to NEW run admissions. Deferring a resume would 500
        ``recover_node`` and lose the resume payload when dispatcher_reconcile
        later re-dispatches it as execute_run with empty resume_data.
        """
        session = AsyncMock()
        run_id = uuid.UUID(RUN_ID)
        org_id = uuid.UUID(ORG_ID)
        with (
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "modulo.db.crud.run.count_active_runs_for_org",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch("modulo.db.crud.run.update_run_status", new_callable=AsyncMock) as update_status,
        ):
            deferred = await dispatch._org_capacity_deferred(session, run_id, org_id, job_type="resume_run")

        assert deferred is False
        update_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admits_when_under_cap(self) -> None:
        with (
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                return_value=5,
            ),
            patch(
                "modulo.db.crud.run.count_active_runs_for_org",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch("modulo.db.crud.run.update_run_status", new_callable=AsyncMock) as update_status,
        ):
            deferred = await dispatch._org_capacity_deferred(AsyncMock(), uuid.UUID(RUN_ID), uuid.UUID(ORG_ID))

        assert deferred is False
        update_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admits_when_no_cap_configured(self) -> None:
        with (
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("modulo.db.crud.run.update_run_status", new_callable=AsyncMock) as update_status,
        ):
            deferred = await dispatch._org_capacity_deferred(AsyncMock(), uuid.UUID(RUN_ID), uuid.UUID(ORG_ID))

        assert deferred is False
        update_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fail_open_when_limit_read_raises(self) -> None:
        with (
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            patch("modulo.db.crud.run.update_run_status", new_callable=AsyncMock) as update_status,
        ):
            deferred = await dispatch._org_capacity_deferred(AsyncMock(), uuid.UUID(RUN_ID), uuid.UUID(ORG_ID))

        assert deferred is False, "fail-open: a reader error must ADMIT the run"
        update_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fail_open_when_count_raises(self) -> None:
        with (
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "modulo.db.crud.run.count_active_runs_for_org",
                new_callable=AsyncMock,
                side_effect=RuntimeError("count failed"),
            ),
            patch("modulo.db.crud.run.update_run_status", new_callable=AsyncMock) as update_status,
        ):
            deferred = await dispatch._org_capacity_deferred(AsyncMock(), uuid.UUID(RUN_ID), uuid.UUID(ORG_ID))

        assert deferred is False, "fail-open: a count error must ADMIT the run"
        update_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_error_reraises_not_fail_open(self) -> None:
        """Cancellation is NOT a fail-open condition: it must propagate.

        The fail-open ``except Exception`` must not swallow
        ``asyncio.CancelledError`` -- a cancelled dispatch must stay cancelled
        so the caller can abort cleanly instead of silently admitting the run.
        """
        with (
            pytest.raises(asyncio.CancelledError),
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
        ):
            await dispatch._org_capacity_deferred(AsyncMock(), uuid.UUID(RUN_ID), uuid.UUID(ORG_ID))


# ---------------------------------------------------------------------------
# _capacity_deferred — per-pipeline max_concurrent_runs gate (plan F3b)
# ---------------------------------------------------------------------------


def _session_with_pipeline(pipeline: object) -> AsyncMock:
    """Session double whose ``execute`` yields *pipeline* for the lookup.

    FAR-1025: ``_capacity_deferred`` resolves the pipeline via
    ``execute(include_soft_deleted(select(Pipeline)...))`` instead of
    ``session.get``, so the result's ``scalar_one_or_none`` must carry it.
    """
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = pipeline
    session.execute = AsyncMock(return_value=result)
    return session


class TestCapacityDeferred:
    @pytest.mark.asyncio
    async def test_missing_run_defers(self) -> None:
        """A run that vanished before the capacity check cannot be enqueued."""
        session = AsyncMock()
        with patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=None):
            deferred = await dispatch._capacity_deferred(session, uuid.UUID(RUN_ID))

        assert deferred is True
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_pipeline_defers(self) -> None:
        """A run whose pipeline was deleted is deferred, never enqueued."""
        run = MagicMock()
        run.pipeline_id = uuid.uuid4()
        session = _session_with_pipeline(None)
        with patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run):
            deferred = await dispatch._capacity_deferred(session, uuid.UUID(RUN_ID))

        assert deferred is True
        session.execute.assert_awaited_once()

    @pytest.mark.parametrize("max_concurrent", [0, -1])
    @pytest.mark.asyncio
    async def test_zero_or_negative_cap_admits(self, max_concurrent: int) -> None:
        """max_concurrent_runs <= 0 means no limit -- never defer on it."""
        run = MagicMock()
        run.pipeline_id = uuid.uuid4()
        pipeline = MagicMock()
        pipeline.max_concurrent_runs = max_concurrent
        session = _session_with_pipeline(pipeline)
        with (
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run),
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock) as count,
        ):
            deferred = await dispatch._capacity_deferred(session, uuid.UUID(RUN_ID))

        assert deferred is False
        count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_at_cap_defers(self) -> None:
        run = MagicMock()
        run.pipeline_id = uuid.uuid4()
        pipeline = MagicMock()
        pipeline.max_concurrent_runs = 2
        session = _session_with_pipeline(pipeline)
        with (
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run),
            patch(
                "modulo.db.crud.run.count_active_runs_for_pipeline",
                new_callable=AsyncMock,
                return_value=2,
            ) as count,
        ):
            deferred = await dispatch._capacity_deferred(session, uuid.UUID(RUN_ID))

        assert deferred is True
        # The run itself is excluded so a resume never counts against its own slot.
        count.assert_awaited_once_with(
            session,
            run.pipeline_id,
            include_pending=False,
            exclude_run_id=uuid.UUID(RUN_ID),
        )

    @pytest.mark.asyncio
    async def test_under_cap_admits(self) -> None:
        run = MagicMock()
        run.pipeline_id = uuid.uuid4()
        pipeline = MagicMock()
        pipeline.max_concurrent_runs = 3
        session = _session_with_pipeline(pipeline)
        with (
            patch("modulo.db.crud.run.get_run", new_callable=AsyncMock, return_value=run),
            patch(
                "modulo.db.crud.run.count_active_runs_for_pipeline",
                new_callable=AsyncMock,
                return_value=1,
            ) as count,
        ):
            deferred = await dispatch._capacity_deferred(session, uuid.UUID(RUN_ID))

        assert deferred is False
        count.assert_awaited_once_with(
            session,
            run.pipeline_id,
            include_pending=False,
            exclude_run_id=uuid.UUID(RUN_ID),
        )


# ---------------------------------------------------------------------------
# dispatch_run org-cap wiring
# ---------------------------------------------------------------------------


class TestDispatchRunOrgCapacity:
    @pytest.mark.asyncio
    async def test_org_cap_deferred_no_enqueue_no_dispatched_at(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=True),
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
    async def test_pipeline_cap_still_defers_regression(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=True),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
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
    async def test_resume_run_at_org_cap_is_admitted(self) -> None:
        """Major 2 regression: a resume_run dispatch at the org cap is ENQUEUED.

        The org-cap internals are set so ``_org_capacity_deferred`` would
        defer an execute_run — but for ``job_type="resume_run"`` it must
        short-circuit to admitted, so ``recover_node`` never sees
        ``("deferred", None)`` (which it treats as an HTTP 500) and the
        ``resume_data`` survives to the worker.
        """
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(return_value=(JOB_ID, False)),
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock) as saq_job,
            patch(
                "modulo.db.crud.run.get_org_run_concurrency_limit",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "modulo.db.crud.run.count_active_runs_for_org",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "modulo.db.crud.run.get_run",
                new_callable=AsyncMock,
                return_value=_run_with_status("running"),
            ),
            patch("modulo.db.crud.run.update_run_status", new_callable=AsyncMock) as update_status,
        ):
            outcome, job_id = await dispatch.dispatch_run(
                RUN_ID,
                ORG_ID,
                job_type="resume_run",
                resume_data={"action": "approved"},
            )

        assert outcome == "enqueued"
        assert job_id == JOB_ID
        saq_job.assert_awaited_once()
        update_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admits_when_both_caps_free(self) -> None:
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(return_value=(JOB_ID, False)),
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock) as saq_job,
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "enqueued"
        assert job_id == JOB_ID
        saq_job.assert_awaited_once()


# ---------------------------------------------------------------------------
# Error enum
# ---------------------------------------------------------------------------


class TestClaimToken:
    def test_token_distinct_from_saq_job_id(self) -> None:
        token = dispatch._new_claim_token()
        job_id = f"saq:job:runs:run:{uuid.uuid4()}"
        assert token
        assert token != job_id
        assert token.isalnum()


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
        # The TOCTOU re-check: no job under the key yet -> enqueue proceeds.
        queue_instance.job = AsyncMock(return_value=None)
        queue_instance.job_id.return_value = JOB_ID
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
        queue_instance.job = AsyncMock(return_value=None)
        queue_instance.job_id.return_value = JOB_ID
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

    @pytest.mark.asyncio
    async def test_enqueue_none_returns_deterministic_deduped_job(self) -> None:
        """``q.enqueue`` returning None means a job with the same key already
        exists -- the caller must report ``deduped`` with the deterministic id.

        The TOCTOU re-check (``q.job(key)``) finds no job under the key, so the
        enqueue proceeds and returns None -- the post-enqueue dedup path.
        """
        enqueue_mock = AsyncMock(return_value=None)
        queue_cls = MagicMock()
        queue_instance = queue_cls.return_value
        queue_instance.enqueue = enqueue_mock
        queue_instance.job = AsyncMock(return_value=None)
        queue_instance.job_id.return_value = JOB_ID
        redis_cls = MagicMock()

        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            patch.object(dispatch, "RedisQueue", queue_cls),
            patch.object(dispatch, "AsyncRedis", redis_cls),
        ):
            job_id, deduped = await dispatch._enqueue_saq(RUN_ID, ORG_ID, "runs", "execute_run", None)

        assert job_id == JOB_ID
        assert deduped is True
        queue_instance.job.assert_awaited_once_with(f"run:{RUN_ID}")
        queue_instance.job_id.assert_called_once_with(f"run:{RUN_ID}")

    @pytest.mark.asyncio
    async def test_key_suffix_suffixed_key_no_dedupe_suppression(self) -> None:
        """A re-dispatch with a FRESH key_suffix enqueues under run:{id}:{suffix}
        — SAQ's key-based dedupe can never suppress the recovery enqueue."""
        enqueue_mock = AsyncMock(return_value=SimpleNamespace(id="saq:job:runs:run:x:abc"))
        queue_cls = MagicMock()
        queue_instance = queue_cls.return_value
        queue_instance.enqueue = enqueue_mock
        queue_instance.job = AsyncMock(return_value=None)
        redis_cls = MagicMock()
        redis_client = redis_cls.from_url.return_value
        redis_client.aclose = AsyncMock(return_value=None)

        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            patch.object(dispatch, "RedisQueue", queue_cls),
            patch.object(dispatch, "AsyncRedis", redis_cls),
        ):
            job_id, deduped = await dispatch._enqueue_saq(RUN_ID, ORG_ID, "runs", "execute_run", None, key_suffix="abc")

        assert deduped is False
        assert job_id == "saq:job:runs:run:x:abc"
        assert enqueue_mock.await_args.kwargs["key"] == f"run:{RUN_ID}:abc"
        # The re-check hit the suffixed key, never a SAQ-internal list.
        queue_instance.job.assert_awaited_once_with(f"run:{RUN_ID}:abc")

    @pytest.mark.asyncio
    async def test_job_now_exists_after_decision_returns_deduped_without_enqueue(self) -> None:
        """TOCTOU re-check (B2): if a job exists under the key AFTER the caller's
        decision, the enqueue is skipped and the deterministic job id returned —
        a concurrent worker already handled it."""
        enqueue_mock = AsyncMock()
        queue_cls = MagicMock()
        queue_instance = queue_cls.return_value
        queue_instance.enqueue = enqueue_mock
        queue_instance.job = AsyncMock(return_value=SimpleNamespace(id=JOB_ID))
        queue_instance.job_id.return_value = JOB_ID
        redis_cls = MagicMock()
        redis_client = redis_cls.from_url.return_value
        redis_client.aclose = AsyncMock(return_value=None)

        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            patch.object(dispatch, "RedisQueue", queue_cls),
            patch.object(dispatch, "AsyncRedis", redis_cls),
        ):
            job_id, deduped = await dispatch._enqueue_saq(RUN_ID, ORG_ID, "runs", "execute_run", None)

        assert deduped is True
        assert job_id == JOB_ID
        enqueue_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# _open_session — shared app engine reuse (single tuned pool)
# ---------------------------------------------------------------------------


class TestOpenSession:
    def test_reuses_shared_app_engine(self) -> None:
        """dispatch must route through the shared tuned engine -- never spawn a
        divergent second pool (the pool-divergence connection churn bug)."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            patch("modulo.api.dependencies.get_or_create_engine") as get_engine,
            patch.object(dispatch, "async_sessionmaker") as sm,
        ):
            session = dispatch._open_session()

        get_engine.assert_called_once()
        sm.assert_called_once_with(get_engine.return_value, expire_on_commit=False, autobegin=False)
        assert session is sm.return_value.return_value


# ---------------------------------------------------------------------------
# _record_dispatched / _record_saq_job / _mark_enqueue_failed — SQL writers
# ---------------------------------------------------------------------------


class TestRecordDispatched:
    @pytest.mark.asyncio
    async def test_writes_dispatched_at(self) -> None:
        """dispatched_at is written BEFORE enqueue (plan F3e) so a crashed
        enqueue leaves a discoverable trace."""
        session = AsyncMock()
        run_id = uuid.UUID(RUN_ID)
        await dispatch._record_dispatched(session, run_id)

        session.execute.assert_awaited_once()
        stmt, params = session.execute.await_args.args
        assert "dispatched_at=now()" in str(stmt)
        assert params["rid"] == run_id


class TestRecordSaqJob:
    @pytest.mark.asyncio
    async def test_writes_dispatcher_job_and_claim_token(self) -> None:
        session = AsyncMock()
        run_id = uuid.UUID(RUN_ID)
        await dispatch._record_saq_job(session, run_id, JOB_ID, "claim-abc")

        session.execute.assert_awaited_once()
        stmt, params = session.execute.await_args.args
        assert "dispatcher='saq'" in str(stmt)
        assert params["rid"] == run_id
        assert params["jid"] == JOB_ID
        assert params["tok"] == "claim-abc"

    @pytest.mark.asyncio
    async def test_preserves_worker_claim_token_when_already_claimed(self) -> None:
        """Critical contract: once a worker claims the run it OWNS the claim
        token. The dispatcher's write must not overwrite it -- a clobbered token
        makes the worker's next heartbeat raise ClaimSupersededError and abort
        the active executor."""
        session = AsyncMock()
        await dispatch._record_saq_job(session, uuid.UUID(RUN_ID), JOB_ID, "claim-abc")

        rendered = str(session.execute.await_args.args[0])
        assert "claim_token = CASE WHEN claim_token IS NULL THEN :tok ELSE claim_token END" in rendered


class TestMarkEnqueueFailed:
    @pytest.mark.asyncio
    async def test_marks_enqueue_failed_non_terminal(self) -> None:
        """A failed enqueue leaves the run PENDING with the non-terminal
        ``enqueue_failed_at`` marker -- never status='failed' -- so
        dispatcher_reconcile can re-dispatch it."""
        session = AsyncMock()
        run_id = uuid.UUID(RUN_ID)
        await dispatch._mark_enqueue_failed(session, run_id)

        session.execute.assert_awaited_once()
        stmt, params = session.execute.await_args.args
        rendered = str(stmt)
        assert "enqueue_failed_at=now()" in rendered
        assert "status='failed'" not in rendered
        assert "error_code='dispatch_failed'" not in rendered
        assert params["rid"] == run_id

    @pytest.mark.asyncio
    async def test_never_overwrites_terminal_runs(self) -> None:
        """A run already complete/cancelled must never be marked enqueue-failed."""
        session = AsyncMock()
        await dispatch._mark_enqueue_failed(session, uuid.UUID(RUN_ID))

        rendered = str(session.execute.await_args.args[0])
        assert "status NOT IN ('complete', 'cancelled')" in rendered


# ---------------------------------------------------------------------------
# _expire_webhook_dedup — retried webhook dedup-hash cleanup
# ---------------------------------------------------------------------------


class TestExpireWebhookDedup:
    @pytest.mark.asyncio
    async def test_no_trigger_event_skips_delete(self) -> None:
        """A run with no trigger event has no dedup hash to clear."""
        session = AsyncMock()
        ev_result = MagicMock()
        ev_result.first.return_value = None
        session.execute = AsyncMock(return_value=ev_result)

        await dispatch._expire_webhook_dedup(session, uuid.UUID(RUN_ID))

        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deletes_matching_dedup_hash(self) -> None:
        """The newest trigger event's (trigger_id, payload_hash) is the pair
        used to expire the dedup hash so a retried webhook is not suppressed."""
        session = AsyncMock()
        ev_result = MagicMock()
        ev_result.first.return_value = ("trigger-1", "hash-1")
        delete_result = MagicMock()
        session.execute = AsyncMock(side_effect=[ev_result, delete_result])

        await dispatch._expire_webhook_dedup(session, uuid.UUID(RUN_ID))

        assert session.execute.await_count == 2
        select_stmt = session.execute.await_args_list[0].args[0]
        delete_stmt = session.execute.await_args_list[1].args[0]
        assert "trigger_events" in str(select_stmt)
        delete_sql = str(delete_stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert "DELETE FROM webhook_dedup_hashes" in delete_sql
        assert "trigger_id" in delete_sql
        assert "payload_hash" in delete_sql


# ---------------------------------------------------------------------------
# Call-site conversions (light mock-based per site)
# ---------------------------------------------------------------------------


class TestCallSiteConversions:
    def test_webhooks_module_uses_dispatch_run(self) -> None:
        from fastapi import BackgroundTasks

        from modulo.api.routes import webhooks

        assert webhooks.dispatch_run is dispatch.dispatch_run
        assert webhooks.BackgroundTasks is BackgroundTasks

    def test_runs_manual_route_uses_dispatch_run(self) -> None:
        from modulo.api.routes import runs

        assert runs.dispatch_run is dispatch.dispatch_run

    def test_mcp_server_uses_dispatch_run(self) -> None:
        import modulo.api.mcp_server as mcp

        assert mcp.dispatch_run is dispatch.dispatch_run


# ---------------------------------------------------------------------------
# dispatcher_reconcile F6a — awaiting_human/claimed gated recovery
# ---------------------------------------------------------------------------
# The reconcile integration suite lives in tests/unit/cron_helpers/
# test_dispatcher_reconcile.py (owned by another tier, NOT in this allowlist),
# so the F6a predicate + discriminator + row loop are exercised here directly
# with mocked rows, following that suite's mocking style.


class _F6aMockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _F6aMockSession:
    """Minimal session double for the dispatcher_reconcile row loop.

    Intercepts the dedicated org-scoped terminalizer UPDATEs (B4 age-bound /
    B5 claim-cap) before the row select and returns ``RETURNING id`` rows from
    ``terminalizer_rows`` (keyed by error_code), so tests can simulate which
    runs were terminalized before re-dispatch.
    """

    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.terminalizer_rows: dict[str, list[uuid.UUID]] = {}
        self.begin_cm = _F6aMockBegin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _F6aMockBegin:
        return self.begin_cm

    def get_bind(self) -> Any:
        return self._get_bind()

    async def flush(self) -> None:
        # Mirror of AsyncSession.flush — called by the FAR-1329 aged-nodeless
        # router after it delegates to _fail_nodeless_run.
        return None

    async def get(self, model: Any, pk: Any) -> SimpleNamespace:
        return SimpleNamespace(max_concurrent_runs=5)

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        s = str(stmt)
        if "set_config" in s:
            return MagicMock()
        if s.startswith("SELECT id FROM runs"):
            # FAR-1329 aged-nodeless router: zero-progress rows at the age
            # bound (none configured here by default).
            r = MagicMock()
            r.all.return_value = [(uid,) for uid in self.terminalizer_rows.get("aged_nodeless", [])]
            return r
        if "UPDATE runs SET" in s:
            ids: list[uuid.UUID] = []
            if "node_attempt_count" in s:
                # FAR-1329 age-bound wedge gate (error code is a bound param).
                ids = self.terminalizer_rows.get("no_progress", [])
            elif "claim_cap_exhausted" in s:
                ids = self.terminalizer_rows.get("claim_cap_exhausted", [])
            r = MagicMock()
            r.all.return_value = [(uid,) for uid in ids]
            r.rowcount = len(ids)
            return r
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


def _f6a_org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _f6a_rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _f6a_row(status: str, *, stale: bool = True) -> SimpleNamespace:
    heartbeat = datetime.now(UTC) - timedelta(minutes=30) if stale else datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        status=status,
        dispatched_at=datetime.now(UTC),
        heartbeat_at=heartbeat,
        node_token_usage={},
        started_at=datetime.now(UTC) - timedelta(minutes=1),
    )


class TestReconcileF6aRecovery:
    @pytest.mark.asyncio
    async def test_awaiting_human_no_job_stale_no_decision_skips_run(self, monkeypatch) -> None:
        """F6a / FAR-541 auto-approve guard: an awaiting_human run with no SAQ
        job + stale heartbeat is NOT re-dispatched as resume_run when no human
        has committed a gate decision for it. Pre-FAR-541 this auto-resumed with
        an empty decision, which the HITL gate treated as an approval (auto-
        approving the gate). The committed-decision guard in
        ``_awaiting_human_has_committed_decision`` now skips genuinely-waiting
        runs so a lost resume job can never silently approve a gate."""
        from modulo.core import cron_helpers as ch

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
        monkeypatch.setenv("SECRET_KEY", "a" * 40)
        monkeypatch.setenv("FERNET_KEY", "b" * 44)

        row = _f6a_row("awaiting_human", stale=True)
        org = uuid.uuid4()
        session = _F6aMockSession(
            [
                _f6a_org_result([org]),
                _f6a_rows_result([row]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = MagicMock()
        q.name = "runs"
        q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
        q.job = AsyncMock(return_value=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_make_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(
                ch,
                "_re_enqueue_run",
                new_callable=AsyncMock,
                return_value=("enqueued", "new-job-id"),
            ) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_awaiting_human_with_live_job_skipped(self, monkeypatch) -> None:
        """F6a gate: an awaiting_human run whose job still exists is NOT
        re-dispatched — the no-job gate is the discriminator."""
        from modulo.core import cron_helpers as ch

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
        monkeypatch.setenv("SECRET_KEY", "a" * 40)
        monkeypatch.setenv("FERNET_KEY", "b" * 44)

        row = _f6a_row("claimed", stale=True)
        org = uuid.uuid4()
        session = _F6aMockSession(
            [
                _f6a_org_result([org]),
                _f6a_rows_result([row]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = MagicMock()
        q.name = "runs"
        q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
        q.job = AsyncMock(return_value=SimpleNamespace(id="saq:job:runs:run:x"))
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_make_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_claim_cap_exhausted_terminalized(self, monkeypatch) -> None:
        """Plan F8/B5: a running SAQ run at its claim cap with a STALE heartbeat
        is FAILED, not re-dispatched — via the dedicated org-scoped terminalizer
        UPDATE, independent of the reconcile predicates."""
        from modulo.core import cron_helpers as ch

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
        monkeypatch.setenv("SECRET_KEY", "a" * 40)
        monkeypatch.setenv("FERNET_KEY", "b" * 44)

        row = _f6a_row("running", stale=True)
        row.claim_count = 20
        org = uuid.uuid4()
        session = _F6aMockSession(
            [
                _f6a_org_result([org]),
                # The capped run was terminalized by the dedicated UPDATE — the
                # subsequent row select returns nothing to re-dispatch.
                _f6a_rows_result([]),
            ]
        )
        session.terminalizer_rows["claim_cap_exhausted"] = [row.id]
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = MagicMock()
        q.name = "runs"
        q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
        q.job = AsyncMock(return_value=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_make_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock) as record_facts,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["claim_cap_terminalized"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        # FAR-162 (P6'): the terminalized run gets a compensating daily fact.
        record_facts.assert_awaited_once_with(row.id, org)

    @pytest.mark.asyncio
    async def test_claim_cap_fresh_heartbeat_not_terminalized(self, monkeypatch) -> None:
        """B5 regression: a capped run with a FRESH heartbeat is a LIVE run on
        its final claim — the terminalizer must NOT fire (stale-heartbeat gate),
        and the run is re-dispatched (no job)."""
        from modulo.core import cron_helpers as ch

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
        monkeypatch.setenv("SECRET_KEY", "a" * 40)
        monkeypatch.setenv("FERNET_KEY", "b" * 44)

        row = _f6a_row("running", stale=False)
        row.claim_count = 20
        org = uuid.uuid4()
        session = _F6aMockSession(
            [
                _f6a_org_result([org]),
                _f6a_rows_result([row]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = MagicMock()
        q.name = "runs"
        q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
        q.job = AsyncMock(return_value=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_make_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(
                ch,
                "_re_enqueue_run",
                new_callable=AsyncMock,
                return_value=("enqueued", "job-2"),
            ) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["claim_cap_terminalized"] == 0
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        ingest.assert_not_awaited()

    def test_reconcile_job_type_discriminator(self) -> None:
        """F6a discriminator: awaiting_human/claimed -> resume_run; else execute_run."""
        from modulo.core import cron_helpers as ch

        assert ch._reconcile_job_type("awaiting_human") == "resume_run"
        assert ch._reconcile_job_type("claimed") == "resume_run"
        assert ch._reconcile_job_type("pending") == "execute_run"
        assert ch._reconcile_job_type("running") == "execute_run"

    def test_reconcile_predicate_contains_f6a_branch(self) -> None:
        """The reconcile predicate includes an awaiting_human/claimed staleness
        branch (F6a), alongside the pending/running branches."""
        from modulo.core import cron_helpers as ch

        pred = ch._build_re_dispatch_predicate(
            reenqueue_window=600,
            stale_window=600,
            capacity_redispatch_seconds=ch.CAPACITY_REDISPATCH_SECONDS,
        )
        rendered = str(pred.compile(compile_kwargs={"literal_binds": True}))
        assert "awaiting_human" in rendered
        assert "claimed" in rendered
        assert "heartbeat_at" in rendered


# ---------------------------------------------------------------------------
# FAR-1584 — bounded row-lock wait on the dispatch-path `runs` writers
# ---------------------------------------------------------------------------
# The dispatch-path UPDATEs on the hot `runs` row (dispatched_at,
# dispatcher='saq', enqueue_failed_at, the admission transaction's org-cap
# demote) used to wait UNBOUNDED on a row lock — the exact statement observed
# blocked in prod (FAR-1524 O11: `UPDATE runs SET dispatched_at=now() WHERE
# id=$1 ... while locking tuple in relation "runs"`). A silent wait past the
# Fly HAProxy 30-minute session window is what the proxy culls
# mid-operation. These tests pin: (1) every writer issues the
# transaction-scoped `set_config('lock_timeout', ..., true)` bound BEFORE its
# row lock, value from Settings.mutation_row_lock_timeout_ms; (2) the value
# AND the operator ceiling stay far below that 1800s window; (3) a 55P03
# expiry is handled NON-SILENTLY with a truthful outcome — the run is never
# lost, never double-dispatched, never a swallowed error; (4) non-lock
# failures still propagate. The real-Postgres contention behaviour (a held
# row lock actually timing out) is an integration concern — the unit seam
# here drives the same 55P03 the bound produces.


class _PgRecordingSession:
    """AsyncSession double reporting the postgresql dialect, recording SQL.

    Takes the LIVE branch of ``set_mutation_row_lock_timeout``'s dialect gate
    (``get_bind().dialect.name == "postgresql"``), so the real bound statement
    is recorded alongside each writer's UPDATE for ORDER/value assertions.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any] | None] = []
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._bind = bind
        self.begin_cm = _MockBegin()

    def get_bind(self) -> MagicMock:
        return self._bind

    def begin(self) -> _MockBegin:
        return self.begin_cm

    async def close(self) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.statements.append(str(stmt))
        self.params.append(params)
        return MagicMock()


def _lock_timeout_error(statement: str) -> OperationalError:
    """Simulated row-lock contention: asyncpg's real ``LockNotAvailableError``
    (SQLSTATE 55P03) wrapped exactly the way SQLAlchemy surfaces it."""
    from asyncpg import exceptions as asyncpg_exceptions

    driver_error = asyncpg_exceptions.LockNotAvailableError("canceling statement due to lock timeout")
    return OperationalError(statement, {}, driver_error)


def _non_lock_db_error() -> OperationalError:
    """A different SQLSTATE (serialization failure) — NOT a lock timeout."""
    return OperationalError("UPDATE runs ...", {}, SimpleNamespace(sqlstate="40001"))


class TestWritersBoundTheirRowLockWait:
    """WIRING + ORDER: every dispatch-path runs writer sets the bound first."""

    @pytest.mark.parametrize(
        ("writer", "args", "update_marker"),
        [
            (dispatch._record_dispatched, (uuid.UUID(RUN_ID),), "dispatched_at=now()"),
            (dispatch._record_saq_job, (uuid.UUID(RUN_ID), JOB_ID, "claim-abc"), "dispatcher='saq'"),
            (dispatch._mark_enqueue_failed, (uuid.UUID(RUN_ID),), "enqueue_failed_at=now()"),
        ],
    )
    @pytest.mark.asyncio
    async def test_transaction_local_lock_bound_precedes_the_update(
        self,
        writer: Any,
        args: tuple[Any, ...],
        update_marker: str,
    ) -> None:
        session = _PgRecordingSession()
        with patch(
            "modulo.db.crud.row_lock.get_settings",
            return_value=_make_settings(mutation_row_lock_timeout_ms=5000),
        ):
            await writer(session, *args)

        bound_at = [i for i, sql in enumerate(session.statements) if "set_config('lock_timeout'" in sql]
        update_at = [i for i, sql in enumerate(session.statements) if update_marker in sql]
        assert bound_at, f"no transaction-local lock_timeout bound issued; statements={session.statements}"
        assert update_at, f"the writer's UPDATE never ran; statements={session.statements}"
        assert bound_at[0] < update_at[0], (
            f"the bound must be set BEFORE the row lock (lock_timeout at {bound_at[0]}, UPDATE at {update_at[0]})"
        )
        # SET LOCAL semantics: set_config(..., is_local => true) — the bound is
        # transaction-scoped and reverts on COMMIT/ROLLBACK, so it can never
        # leak onto a pooled connection's later statements.
        assert ", true)" in session.statements[bound_at[0]]
        # The value comes from the operator knob, not a hardcoded literal.
        bound_params = session.params[bound_at[0]]
        assert bound_params is not None
        assert bound_params["val"] == "5000ms"

    @pytest.mark.asyncio
    async def test_admission_transaction_sets_the_bound_as_its_first_statement(self) -> None:
        """The admission transaction bounds itself FIRST: its org-cap demote
        (``update_run_status``) is a writer on the same hot ``runs`` row."""
        session = _PgRecordingSession()
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            patch.object(dispatch, "_open_session", return_value=session),
            _rls_patch(),
            patch(
                "modulo.db.crud.run.get_run",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(status="pending"),
            ),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=True),
            patch(
                "modulo.db.crud.row_lock.get_settings",
                return_value=_make_settings(mutation_row_lock_timeout_ms=5000),
            ),
        ):
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "deferred"
        assert job_id is None
        assert session.statements, "the admission transaction executed no statements"
        assert "set_config('lock_timeout'" in session.statements[0]
        first_params = session.params[0]
        assert first_params is not None
        assert first_params["val"] == "5000ms"


class TestLockBoundBelowHaproxyWindow:
    """Pin the FAR-1524 relationship: the bound is well below the Fly HAProxy
    30-minute session window (``timeout client/server 30m`` = 1800s), so a
    lock wait can never survive long enough to be culled mid-operation."""

    _HAPROXY_SESSION_WINDOW_SECONDS = 1800

    def test_default_value_is_well_below_the_window(self) -> None:
        from modulo.settings import Settings

        default_ms = Settings.model_fields["mutation_row_lock_timeout_ms"].default
        assert default_ms == 5000
        assert default_ms < self._HAPROXY_SESSION_WINDOW_SECONDS * 1000

    def test_operator_ceiling_is_well_below_the_window(self) -> None:
        from modulo.settings import Settings

        field = Settings.model_fields["mutation_row_lock_timeout_ms"]
        ceilings = [m.le for m in field.metadata if getattr(m, "le", None) is not None]
        assert ceilings, f"no le() ceiling on mutation_row_lock_timeout_ms: {field.metadata}"
        ceiling_ms = max(ceilings)
        # Even the MAXIMUM operator-configured value (30s) stays far below the
        # window — no configuration of this knob can reach the cull threshold.
        assert ceiling_ms < self._HAPROXY_SESSION_WINDOW_SECONDS * 1000


class TestLockTimeoutHandling:
    """A 55P03 expiry of the bounded wait: logged, truthful outcome, run never lost."""

    @pytest.mark.asyncio
    async def test_dispatched_write_lock_timeout_defers_without_enqueue(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """THE FAR-1524-O11 case: the dispatched_at UPDATE meets a held runs
        row lock and the BOUND fires (55P03).

        The write rolled back, so the run stays ``pending`` +
        ``dispatched_at IS NULL`` — the state ``dispatcher_reconcile``'s
        capacity_deferred branch re-dispatches on its next tick. The outcome
        is ``deferred`` (every dispatch_run caller already treats that as "not
        dispatched, recovered by reconcile"), the enqueue never happens (no
        double-dispatch), and the condition is logged at WARNING with the full
        exception chain — never a silent no-op, never a lost run.
        """
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_record_dispatched",
                new_callable=AsyncMock,
                side_effect=_lock_timeout_error("UPDATE runs SET dispatched_at=now()"),
            ),
            _enqueue_patch() as enqueue,
        ):
            caplog.set_level(logging.WARNING, logger="modulo.core.dispatch")
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "deferred"
        assert job_id is None
        enqueue.assert_not_awaited()
        assert "55P03" in caplog.text
        assert RUN_ID in caplog.text

    @pytest.mark.asyncio
    async def test_admission_write_lock_timeout_defers_without_enqueue(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A 55P03 on the admission transaction's org-cap demote write (same
        hot row): rolled back, deferred, nothing enqueued, logged."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(
                dispatch,
                "_org_capacity_deferred",
                new_callable=AsyncMock,
                side_effect=_lock_timeout_error("UPDATE runs SET error_code"),
            ),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            _enqueue_patch() as enqueue,
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock) as dispatched,
        ):
            caplog.set_level(logging.WARNING, logger="modulo.core.dispatch")
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "deferred"
        assert job_id is None
        enqueue.assert_not_awaited()
        dispatched.assert_not_awaited()
        assert "55P03" in caplog.text

    @pytest.mark.asyncio
    async def test_saq_job_record_lock_timeout_still_reports_enqueued(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Commit-then-log: by the time dispatcher='saq' is recorded the job
        is ALREADY on the queue, so a 55P03 there must not fail the dispatch
        (that would report an error for a run that will execute). The skipped
        bookkeeping lands the row on the reconcile zombie branch; the atomic
        claim keeps execution at-most-once."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(return_value=(JOB_ID, False)),
            patch.object(
                dispatch,
                "_record_saq_job",
                new_callable=AsyncMock,
                side_effect=_lock_timeout_error("UPDATE runs SET dispatcher='saq'"),
            ),
        ):
            caplog.set_level(logging.WARNING, logger="modulo.core.dispatch")
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "enqueued"
        assert job_id == JOB_ID
        assert "55P03" in caplog.text
        assert RUN_ID in caplog.text

    @pytest.mark.asyncio
    async def test_enqueue_failed_marker_lock_timeout_still_reports_enqueue_failed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The enqueue itself failed (that part stays TRUE — outcome
        ``enqueue_failed``), but the marker write's 55P03 must not stack an
        unhandled exception on top of it. The row then matches the reconcile
        zombie branch (no marker) instead of the heartbeat-gated
        enqueue_failed branch — re-dispatched either way."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(dispatch, "_record_dispatched", new_callable=AsyncMock),
            _enqueue_patch(side_effect=RuntimeError("redis down")),
            patch.object(dispatch.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(
                dispatch,
                "_mark_enqueue_failed",
                new_callable=AsyncMock,
                side_effect=_lock_timeout_error("UPDATE runs SET enqueue_failed_at"),
            ),
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock),
        ):
            caplog.set_level(logging.WARNING, logger="modulo.core.dispatch")
            outcome, job_id = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert outcome == "enqueue_failed"
        assert job_id is None
        assert "55P03" in caplog.text

    @pytest.mark.asyncio
    async def test_lock_timed_out_dispatch_recovers_on_the_next_attempt(self) -> None:
        """Retried, not lost, not double-dispatched.

        Attempt 1 hits the bounded 55P03 -> ``deferred`` with NO enqueue and
        NO ``dispatched_at`` written (the pending/dispatched_at-NULL state
        reconcile re-dispatches). Attempt 2 (the next tick, lock released)
        writes and enqueues exactly ONCE.
        """
        first_attempt_error = _lock_timeout_error("UPDATE runs SET dispatched_at=now()")
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_record_dispatched",
                new_callable=AsyncMock,
                side_effect=[first_attempt_error, None],
            ),
            _enqueue_patch(return_value=(JOB_ID, False)) as enqueue,
            patch.object(dispatch, "_record_saq_job", new_callable=AsyncMock),
        ):
            first_outcome, first_job = await dispatch.dispatch_run(RUN_ID, ORG_ID)
            second_outcome, second_job = await dispatch.dispatch_run(RUN_ID, ORG_ID)

        assert first_outcome == "deferred"
        assert first_job is None
        assert second_outcome == "enqueued"
        assert second_job == JOB_ID
        # Only the successful attempt enqueued — the lock-timed-out attempt
        # never reached the queue, so there is exactly one job.
        assert enqueue.await_count == 1

    def test_deferred_lock_timeout_state_matches_the_reconcile_redispatch_branch(self) -> None:
        """Structural link: ``deferred`` after a 55P03 leaves the run ``pending``
        + ``dispatched_at IS NULL``, and that IS the predicate branch
        dispatcher_reconcile re-dispatches every tick — 'retried on the next
        dispatch tick' is enforced by the existing recovery query, not assumed.
        """
        from modulo.core import cron_helpers as ch

        pred = ch._build_re_dispatch_predicate(
            reenqueue_window=600,
            stale_window=600,
            capacity_redispatch_seconds=ch.CAPACITY_REDISPATCH_SECONDS,
        )
        rendered = str(pred.compile(compile_kwargs={"literal_binds": True}))
        assert "dispatched_at IS NULL" in rendered
        assert "status" in rendered

    @pytest.mark.asyncio
    async def test_non_lock_db_error_still_propagates(self) -> None:
        """The handler is targeted at 55P03 ONLY: a serialization failure
        (40001) from the same statement keeps its loud failure path — no
        over-broad swallow of every DB error."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_record_dispatched",
                new_callable=AsyncMock,
                side_effect=_non_lock_db_error(),
            ),
            pytest.raises(OperationalError),
        ):
            await dispatch.dispatch_run(RUN_ID, ORG_ID)

    @pytest.mark.asyncio
    async def test_unrelated_exception_still_propagates(self) -> None:
        """A non-SQL failure (connection lost) is not a lock timeout and must
        keep propagating to the caller's own error handling."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_record_dispatched",
                new_callable=AsyncMock,
                side_effect=RuntimeError("connection lost"),
            ),
            pytest.raises(RuntimeError, match="connection lost"),
        ):
            await dispatch.dispatch_run(RUN_ID, ORG_ID)


class TestLockTimeoutWrappersCancellationAndPassthrough:
    """FAR-1584: the two single-session wrappers and ``dispatch_run`` itself.

    ``asyncio.CancelledError`` inherits from ``BaseException`` (not
    ``Exception``), so every handler re-raises it UNCONDITIONALLY — a cancelled
    dispatch must never be mistaken for a handled lock timeout, and the
    ``finally`` still closes the session. The targeted handler must equally
    keep re-raising every NON-lock failure: the optimisation is scoped to
    55P03 and nothing broader.
    """

    @pytest.mark.asyncio
    async def test_mark_enqueue_failed_session_reraises_cancelled_error(self) -> None:
        with (
            _rls_patch(),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_mark_enqueue_failed",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock),
            pytest.raises(asyncio.CancelledError),
        ):
            await dispatch._mark_enqueue_failed_session(uuid.UUID(RUN_ID), uuid.UUID(ORG_ID))

    @pytest.mark.asyncio
    async def test_mark_enqueue_failed_session_reraises_non_lock_error(self) -> None:
        with (
            _rls_patch(),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_mark_enqueue_failed",
                new_callable=AsyncMock,
                side_effect=_non_lock_db_error(),
            ),
            patch.object(dispatch, "_expire_webhook_dedup", new_callable=AsyncMock),
            pytest.raises(OperationalError),
        ):
            await dispatch._mark_enqueue_failed_session(uuid.UUID(RUN_ID), uuid.UUID(ORG_ID))

    @pytest.mark.asyncio
    async def test_record_saq_job_session_reraises_cancelled_error(self) -> None:
        with (
            _rls_patch(),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_record_saq_job",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await dispatch._record_saq_job_session(uuid.UUID(RUN_ID), uuid.UUID(ORG_ID), JOB_ID)

    @pytest.mark.asyncio
    async def test_record_saq_job_session_reraises_non_lock_error(self) -> None:
        with (
            _rls_patch(),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_record_saq_job",
                new_callable=AsyncMock,
                side_effect=_non_lock_db_error(),
            ),
            pytest.raises(OperationalError),
        ):
            await dispatch._record_saq_job_session(uuid.UUID(RUN_ID), uuid.UUID(ORG_ID), JOB_ID)

    @pytest.mark.asyncio
    async def test_dispatch_run_admission_reraises_cancelled_error(self) -> None:
        """A cancellation inside the admission transaction propagates — it is
        not a bounded-lock expiry, and no enqueue attempt is made."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(
                dispatch,
                "_org_capacity_deferred",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            _enqueue_patch() as enqueue,
            pytest.raises(asyncio.CancelledError),
        ):
            await dispatch.dispatch_run(RUN_ID, ORG_ID)
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_run_admission_reraises_non_lock_error(self) -> None:
        """A NON-lock DB failure in the admission transaction still propagates:
        the deferred-return branch is scoped to 55P03 only, never to every
        database error."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(
                dispatch,
                "_org_capacity_deferred",
                new_callable=AsyncMock,
                side_effect=_non_lock_db_error(),
            ),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            _enqueue_patch() as enqueue,
            pytest.raises(OperationalError),
        ):
            await dispatch.dispatch_run(RUN_ID, ORG_ID)
        enqueue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatch_run_dispatched_write_reraises_cancelled_error(self) -> None:
        """A cancellation while writing dispatched_at propagates — the run must
        be reported as not-dispatched by the caller's cancellation handling,
        and the queue is never touched."""
        with (
            patch.object(dispatch, "get_settings", return_value=_make_settings()),
            _rls_patch(),
            patch.object(dispatch, "_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_org_capacity_deferred", new_callable=AsyncMock, return_value=False),
            patch.object(dispatch, "_open_session", return_value=_MockSession()),
            patch.object(
                dispatch,
                "_record_dispatched",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            _enqueue_patch() as enqueue,
            pytest.raises(asyncio.CancelledError),
        ):
            await dispatch.dispatch_run(RUN_ID, ORG_ID)
        enqueue.assert_not_awaited()
