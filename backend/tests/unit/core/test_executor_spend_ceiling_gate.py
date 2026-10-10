"""Regression tests for the FAR-391 executor pre-gate (Major 1 of the PR review).

The pre-gate must terminalize a run as ``cost_ceiling_exceeded`` BEFORE any
billable work when the org has zero remaining budget — i.e. an org exactly AT
its ceiling (cumulative == spend_ceiling) or a kill-switch ceiling of 0. The
gate achieves this by evaluating the org ceiling with a minimal 1-cent next-step
charge so ``cumulative >= spend_ceiling`` is refused (the finalize ledger block
keeps the stricter ``>`` comparison for the authoritative billing refusal).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.executor import (
    FINALIZE_LOCK_RETRY_ATTEMPTS,
    PipelineExecutor,
    _is_finalize_lock_retryable,
)
from modulo.core.spend_ceiling import ORG_CEILING_EXCEEDED
from modulo.db.crud.run_node_outputs import DualWriteError
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run


@asynccontextmanager
async def _acm(obj):
    yield obj


def _make_session(org: Organisation | None) -> MagicMock:
    session = AsyncMock()

    def _execute(stmt):
        result = MagicMock()
        text = str(stmt).lower()
        result.scalar_one_or_none = MagicMock(return_value=org if "organisations" in text else None)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    # ``session.begin()`` is used as an async context manager in the gate.
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_self(org: Organisation | None, run: Run | None) -> tuple[MagicMock, MagicMock]:
    session = _make_session(org)
    fake_self = MagicMock()
    fake_self._session_factory = MagicMock(return_value=_acm(session))
    fake_self._log = MagicMock()
    return fake_self, session


def _org(*, spend_ceiling_cents, org_cumulative_spend_cents=0) -> Organisation:
    org = MagicMock(spec=Organisation)
    org.id = uuid.uuid4()
    org.spend_ceiling_cents = spend_ceiling_cents
    org.org_cumulative_spend_cents = org_cumulative_spend_cents
    return org


def _run() -> Run:
    run = MagicMock(spec=Run)
    run.id = uuid.uuid4()
    return run


async def test_pre_gate_blocks_when_at_ceiling() -> None:
    org = _org(spend_ceiling_cents=5000, org_cumulative_spend_cents=5000)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is not None
    update_status.assert_awaited_once()
    assert update_status.call_args.kwargs["error_code"] == ORG_CEILING_EXCEEDED


async def test_pre_gate_blocks_kill_switch_zero_ceiling() -> None:
    org = _org(spend_ceiling_cents=0, org_cumulative_spend_cents=0)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is not None
    update_status.assert_awaited_once()


async def test_pre_gate_allows_when_budget_remaining() -> None:
    org = _org(spend_ceiling_cents=5000, org_cumulative_spend_cents=4999)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is None
    update_status.assert_not_awaited()


async def test_pre_gate_allows_when_no_ceiling() -> None:
    org = _org(spend_ceiling_cents=None, org_cumulative_spend_cents=0)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is None
    update_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fix 2 — bounded whole-transaction finalisation lock retry (40P01 / 55P03)
# ---------------------------------------------------------------------------


class _SqlstateError(Exception):
    """A stand-in DBAPI error carrying a SQLSTATE for the retry classifier."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate={sqlstate}")
        self.sqlstate = sqlstate


def _finalize_self(session: MagicMock) -> MagicMock:
    fake_self = MagicMock()
    # A FRESH session context manager per attempt — a single reused
    # ``@asynccontextmanager`` cannot be entered twice (the retry re-enters it).
    fake_self._session_factory = MagicMock(side_effect=lambda: _acm(session))
    fake_self._claim_token = None
    return fake_self


async def _run_finalize(fake_self: MagicMock, *, work_intact: bool | None = None) -> None:
    await PipelineExecutor._run_finalize_cost_transaction(
        fake_self,
        run_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        final_status="complete",
        error_code=None,
        error_detail=None,
        node_token_usage=None,
        completed_node_outputs={},
        node_type_map={},
        work_intact=work_intact,
    )


def test_finalize_lock_retryable_classifier() -> None:
    assert _is_finalize_lock_retryable(_SqlstateError("40P01")) is True  # deadlock
    assert _is_finalize_lock_retryable(_SqlstateError("55P03")) is True  # lock_timeout
    assert _is_finalize_lock_retryable(_SqlstateError("23505")) is False  # unique violation
    assert _is_finalize_lock_retryable(ValueError("nope")) is False


def test_dual_write_error_with_deadlock_sqlstate_is_not_retryable() -> None:
    """MAJOR 4: ``DualWriteError`` is a fail-closed abort raised AFTER the run is
    terminalised ``dual_write_failed``. Even when it carries a 40P01 SQLSTATE,
    retrying would overwrite that truthful status — it must NOT be retryable."""
    dwe = DualWriteError(
        "new-table leg deadlocked",
        run_id=uuid.uuid4(),
        organisation_id=None,
        sqlstate="40P01",
    )
    assert dwe.sqlstate == "40P01"  # the SQLSTATE that would otherwise match
    assert _is_finalize_lock_retryable(dwe) is False


async def test_finalize_txn_retries_deadlock_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _make_session(None)
    fake_self = _finalize_self(session)
    monkeypatch.setattr("modulo.core.pipeline_engine.executor._FINALIZE_LOCK_RETRY_DELAY_SECONDS", 0.0)
    finalize_mock = AsyncMock(side_effect=[_SqlstateError("40P01"), None])

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=finalize_mock),
    ):
        await _run_finalize(fake_self)

    assert finalize_mock.await_count == 2, "a deadlock must re-run the whole transaction once"


async def test_finalize_txn_retries_lock_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _make_session(None)
    fake_self = _finalize_self(session)
    monkeypatch.setattr("modulo.core.pipeline_engine.executor._FINALIZE_LOCK_RETRY_DELAY_SECONDS", 0.0)
    finalize_mock = AsyncMock(side_effect=[_SqlstateError("55P03"), None])

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=finalize_mock),
    ):
        await _run_finalize(fake_self)

    assert finalize_mock.await_count == 2, "a bounded lock timeout must re-run the whole transaction once"


async def test_finalize_txn_persistent_deadlock_raises_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(None)
    fake_self = _finalize_self(session)
    monkeypatch.setattr("modulo.core.pipeline_engine.executor._FINALIZE_LOCK_RETRY_DELAY_SECONDS", 0.0)
    finalize_mock = AsyncMock(side_effect=_SqlstateError("40P01"))

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=finalize_mock),
        pytest.raises(_SqlstateError),
    ):
        await _run_finalize(fake_self)

    # Bounded: it must give up (and propagate to the truthful executor terminal)
    # rather than loop forever.
    assert finalize_mock.await_count == FINALIZE_LOCK_RETRY_ATTEMPTS


async def test_finalize_txn_non_retryable_error_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _make_session(None)
    fake_self = _finalize_self(session)
    monkeypatch.setattr("modulo.core.pipeline_engine.executor._FINALIZE_LOCK_RETRY_DELAY_SECONDS", 0.0)
    finalize_mock = AsyncMock(side_effect=ValueError("genuine failure"))

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=finalize_mock),
        pytest.raises(ValueError, match="genuine failure"),
    ):
        await _run_finalize(fake_self)

    assert finalize_mock.await_count == 1, "a non-retryable failure must not be retried"


async def test_finalize_txn_zero_attempts_runs_no_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: a retry budget of zero attempts enters the loop zero times and
    runs no finalisation at all (it must not call ``finalize_cost``)."""
    session = _make_session(None)
    fake_self = _finalize_self(session)
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.FINALIZE_LOCK_RETRY_ATTEMPTS", 0)
    finalize_mock = AsyncMock()

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=finalize_mock),
    ):
        await _run_finalize(fake_self)

    finalize_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-1642 item 3 — retry-idempotent metric emission
# ---------------------------------------------------------------------------


async def test_finalize_retry_flushes_buffered_metrics_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock-aborted attempt's buffered counter must be discarded; only the
    committing attempt's counter flushes — so one re-run emits ONE sample, not
    one per attempt.

    ``finalize_cost`` is replaced with a stand-in that buffers a
    ``limit_refused`` on the attempt's sink and aborts the first attempt with a
    deadlock (40P01). The sink is executor-owned: the failed attempt's sink must
    never flush, and the successful attempt's must flush exactly once.
    """
    session = _make_session(None)
    fake_self = _finalize_self(session)
    monkeypatch.setattr("modulo.core.pipeline_engine.executor._FINALIZE_LOCK_RETRY_DELAY_SECONDS", 0.0)

    sinks: list[object] = []

    async def _finalize(session: object, **kwargs: object) -> None:
        sink = kwargs["metric_sink"]
        sinks.append(sink)
        assert sink is not None
        sink.limit_refused("team-a")  # type: ignore[attr-defined]
        if len(sinks) == 1:
            raise _SqlstateError("40P01")

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=_finalize),
        patch("modulo.core.cost_controller.finalize.record_limit_refused") as limit_refused,
    ):
        await _run_finalize(fake_self)

    assert len(sinks) == 2, "the deadlock must re-run the whole transaction exactly once"
    assert sinks[0] is not sinks[1], "each attempt must get a FRESH sink"
    limit_refused.assert_called_once_with("team-a")


async def test_finalize_persistent_failure_flushes_no_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every attempt aborts, nothing committed — so no buffered counter
    may be flushed (a metric must never count a finalisation that rolled back)."""
    session = _make_session(None)
    fake_self = _finalize_self(session)
    monkeypatch.setattr("modulo.core.pipeline_engine.executor._FINALIZE_LOCK_RETRY_DELAY_SECONDS", 0.0)

    async def _finalize(session: object, **kwargs: object) -> None:
        sink = kwargs["metric_sink"]
        assert sink is not None
        sink.limit_refused("team-a")  # type: ignore[attr-defined]
        raise _SqlstateError("40P01")

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=_finalize),
        patch("modulo.core.cost_controller.finalize.record_limit_refused") as limit_refused,
        pytest.raises(_SqlstateError),
    ):
        await _run_finalize(fake_self)

    limit_refused.assert_not_called()


# ---------------------------------------------------------------------------
# work_intact write failures inside the owned finalisation transaction
# ---------------------------------------------------------------------------


async def test_finalize_work_intact_cancelled_propagates() -> None:
    """A ``CancelledError`` from the work_intact write must PROPAGATE, never be
    swallowed as a best-effort failure — it re-raises through BOTH the inner
    work_intact guard and the outer transaction guard."""
    session = _make_session(None)
    fake_self = _finalize_self(session)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch(
            "modulo.core.pipeline_engine.executor._apply_work_intact",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _run_finalize(fake_self, work_intact=True)


async def test_finalize_work_intact_write_error_is_swallowed() -> None:
    """A non-cancelled work_intact write failure is best-effort: it is logged
    and swallowed so the finalisation still completes, and the reclassify step
    that depends on a successful write is NOT run."""
    session = _make_session(None)
    fake_self = _finalize_self(session)
    reclassify = AsyncMock()
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch(
            "modulo.core.pipeline_engine.executor._apply_work_intact",
            new=AsyncMock(side_effect=RuntimeError("work_intact write failed")),
        ),
        patch("modulo.core.pipeline_engine.executor._reclassify_after_work_intact", new=reclassify),
    ):
        await _run_finalize(fake_self, work_intact=True)

    reclassify.assert_not_awaited()
