"""Unit tests for the FAR-391 hard spend-ceiling enforcement in the terminal ledger block.

Verifies that ``_ledger_block`` refuses the ledger (and terminalizes the run as
``cost_ceiling_exceeded``) when the per-run or per-org ceiling is breached, and
that on success the org's consumed total is incremented. DB is fully mocked.

FAR-1833: the ceiling/cumulative state now lives on ``organisation_cost_state``
(read via ``get_org_cost_state``) and the accrual is a single atomic
``increment_org_cumulative_spend`` statement applied inside the ledger
savepoint. The gate's serialisation point is the per-org cost-state advisory
lock (``lock_org_cost_state``), NOT an ``organisations`` row lock.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller.finalize import (
    FinalizeMetricSink,
    _emit_duplicate_terminal,
    _emit_finalize_deferred,
    _emit_limit_refused,
    _fallback_finalize,
    _handle_ledger_write_failure,
    _ledger_block,
    _LedgerEscapeContext,
    _MergedSets,
    _record_ledger_with_retry,
)
from modulo.core.spend_ceiling import ORG_CEILING_EXCEEDED, RUN_CEILING_EXCEEDED
from modulo.db.models.organisation import Organisation
from modulo.db.models.organisation_cost_state import OrganisationCostState
from modulo.db.models.run import Run


@asynccontextmanager
async def _acm(obj):
    """A minimal async context manager yielding *obj* (for fresh-txn escapes)."""
    yield obj


def _make_run() -> MagicMock:
    run = MagicMock(spec=Run)
    run.id = uuid.uuid4()
    run.ledger_written = False
    run.ledger_refused_at = None
    run.status = "complete"
    run.error_code = None
    run.error_detail = None
    run.pipeline_id = uuid.uuid4()
    run.owner_team_id = None
    return run


def _make_org() -> MagicMock:
    org = MagicMock(spec=Organisation)
    org.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    org.triggers_paused = False
    org.settings_json = {}
    return org


def _make_state(*, max_run_cost_cents=None, spend_ceiling_cents=None, org_cumulative_spend_cents=0) -> MagicMock:
    state = MagicMock(spec=OrganisationCostState)
    state.max_run_cost_cents = max_run_cost_cents
    state.spend_ceiling_cents = spend_ceiling_cents
    state.org_cumulative_spend_cents = org_cumulative_spend_cents
    return state


def _cost_state(*, max_run_cost_cents=None, spend_ceiling_cents=None, org_cumulative_spend_cents=0, present=True):
    """Patch the cost-state read the ceiling gate uses.

    ``present=False`` models an org with no cost-state row (the defaults)."""
    state = (
        _make_state(
            max_run_cost_cents=max_run_cost_cents,
            spend_ceiling_cents=spend_ceiling_cents,
            org_cumulative_spend_cents=org_cumulative_spend_cents,
        )
        if present
        else None
    )
    return patch("modulo.core.cost_controller.finalize.get_org_cost_state", new=AsyncMock(return_value=state))


def _accrual():
    """Patch the atomic cost-state accrual so unit tests never touch the DB."""
    return patch("modulo.core.cost_controller.finalize.increment_org_cumulative_spend", new=AsyncMock())


async def test_spend_ceiling_gate_takes_cost_state_advisory_lock_not_org_row() -> None:
    """FAR-1833: the ceiling gate must serialise on the per-org cost-state
    advisory lock (``lock_org_cost_state``) and must NOT request a row lock on
    the ``organisations`` row.

    The old FAR-1624 A22 ``with_for_update(key_share=True)`` org-row lock
    coupled the finalize path with the ceiling endpoints' org-row write — the
    D9 deadlock. The gate now reads the org row only for existence, takes the
    cost advisory, and reads ``organisation_cost_state`` for the values.
    """
    from sqlalchemy.dialects import postgresql

    from modulo.core.cost_controller.finalize import _apply_spend_ceiling_gate

    org = _make_org()
    captured: list[object] = []
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    session = AsyncMock()
    session.flush = AsyncMock()

    async def _execute(stmt: object) -> MagicMock:
        captured.append(stmt)
        return result

    session.execute = AsyncMock(side_effect=_execute)

    run = _make_run()
    with (
        patch("modulo.core.cost_controller.finalize.lock_org_cost_state", new=AsyncMock()) as lock_cost,
        _cost_state(present=True),
    ):
        skip_ledger, accrual_cents = await _apply_spend_ceiling_gate(
            session,
            run,
            org_id=org.id,
            total=Decimal("1.00"),
            run_id=run.id,
        )

    assert skip_ledger is False
    assert accrual_cents == 100
    lock_cost.assert_awaited_once_with(session, org.id)
    # The only org-row statement is a plain SELECT — no FOR UPDATE / FOR NO KEY
    # UPDATE anywhere in the compiled SQL.
    org_statements = [
        str(stmt.compile(dialect=postgresql.dialect())) for stmt in captured if "organisations" in str(stmt)
    ]
    assert org_statements, "the org row must still be read for existence"
    assert all("FOR UPDATE" not in sql and "FOR NO KEY UPDATE" not in sql for sql in org_statements)


def _session_for(run: MagicMock, org: MagicMock) -> AsyncMock:
    """A session whose execute returns the Run (FOR UPDATE) then the Org (plain read)."""

    def _execute(stmt, *_args, **_kwargs):
        text = str(stmt)
        result = MagicMock()
        if "organisations" in text:
            result.scalar_one_or_none = MagicMock(return_value=org)
            result.scalar_one = MagicMock(return_value=org)
        else:
            result.scalar_one = MagicMock(return_value=run)
            result.scalar_one_or_none = MagicMock(return_value=run)
        return result

    s = AsyncMock()
    s.execute = AsyncMock(side_effect=_execute)
    s.flush = AsyncMock()
    return s


async def test_org_ceiling_exceeded_refuses_ledger_and_halts_run() -> None:
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)

    with _cost_state(spend_ceiling_cents=100, org_cumulative_spend_cents=100):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("2.00"),  # would push cumulative to $3.00 > $1.00 ceiling
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    assert run.ledger_refused_at is not None
    assert run.status == "cost_ceiling_exceeded"
    assert run.error_code == ORG_CEILING_EXCEEDED


async def test_run_ceiling_exceeded_refuses_ledger() -> None:
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)

    with _cost_state(max_run_cost_cents=100, org_cumulative_spend_cents=0):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("2.00"),  # single run cost $2.00 > $1.00 cap
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    assert run.ledger_refused_at is not None
    assert run.status == "cost_ceiling_exceeded"
    assert run.error_code == RUN_CEILING_EXCEEDED


async def test_ceiling_refusal_preserves_explicit_cancelled_status() -> None:
    """FAR-391 regression — Minor 1: an explicit terminal CANCEL must not be
    overwritten by the ceiling refusal (which would feed the wrong status to
    journey advancement). The ledger is still refused, but the status stays
    ``cancelled``.
    """
    run = _make_run()
    run.status = "cancelled"
    org = _make_org()
    session = _session_for(run, org)

    with _cost_state(spend_ceiling_cents=100, org_cumulative_spend_cents=100):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="cancelled",
            total=Decimal("2.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    # Refused (no billing beyond the ceiling) ...
    assert run.ledger_refused_at is not None
    # ... but the explicit cancel status is preserved, not overwritten.
    assert run.status == "cancelled"
    # The cancel branch leaves error_code untouched (it was None before).
    assert run.error_code is None


async def test_org_row_missing_skips_gate_and_ledger_write() -> None:
    """FAR-1025: an org row that vanished is treated as no ceiling — the gate
    returns ``(False, 0)`` so the ledger write proceeds without an accrual."""
    from modulo.core.cost_controller.finalize import _apply_spend_ceiling_gate

    run = _make_run()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    with patch("modulo.core.cost_controller.finalize.lock_org_cost_state", new=AsyncMock()):
        skip_ledger, accrual_cents = await _apply_spend_ceiling_gate(
            session,
            run,
            org_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            total=Decimal("1.00"),
            run_id=run.id,
        )

    assert skip_ledger is False
    assert accrual_cents == 0


async def test_within_ceilings_increments_org_cumulative() -> None:
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)

    with (
        _cost_state(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500),
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(return_value=(True, None)),
        ),
        # The best-effort circuit-breaker check is out of scope here and would
        # otherwise issue real queries against the mock session.
        patch("modulo.core.cost_controller.finalize._check_circuit_breaker", new=AsyncMock()),
        _accrual() as accrual,
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),  # $3.00 run
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    # Only a SUCCESSFUL ledger write accrues the org's consumed total (the
    # accrual rides the ledger savepoint); exactly one atomic increment of the
    # $3.00 run cost.
    assert run.ledger_refused_at is None
    assert run.ledger_written is True
    accrual.assert_awaited_once_with(session, org.id, delta_cents=300)


async def test_ledger_write_failure_then_refinalize_does_not_double_accrue() -> None:
    """Money-correctness regression: a contained (non-abort) ledger-write failure
    must NOT commit the org accrual, so a later re-finalisation of the SAME run
    accrues exactly once (the ``ledger_written`` guard does not cover the
    accrual)."""
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)

    # First finalisation: every ledger-write attempt fails (non-abort). The
    # savepoint rolls back, so the accrual must NOT commit.
    with (
        _cost_state(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500),
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(side_effect=RuntimeError("ledger write failed")),
        ),
        _accrual() as accrual,
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    # The failure was BEFORE the increment: no accrual was applied at all.
    accrual.assert_not_awaited()
    assert run.ledger_written is False  # left re-finalisable by the reduced escape

    # Re-finalisation: the ledger write now succeeds -> accrue exactly once.
    with (
        _cost_state(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500),
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("modulo.core.cost_controller.finalize._check_circuit_breaker", new=AsyncMock()),
        _accrual() as accrual,
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    assert run.ledger_written is True
    accrual.assert_awaited_once_with(session, org.id, delta_cents=300)


# ---------------------------------------------------------------------------
# MAJOR 2 — a retry re-applies the atomic accrual without a stale ORM read
# ---------------------------------------------------------------------------


async def test_accrual_retry_reapplies_atomic_increment_without_orm_reread() -> None:
    """A savepoint rollback expires any ORM state; the retry re-runs the accrual.

    FAR-1833: the accrual is a single atomic ``increment_org_cumulative_spend``
    statement, so there is no org attribute to re-read (the old
    ``_read_accrued_base`` / ``MissingGreenlet`` hazard is gone). On a contained
    failure the savepoint rolls the increment back; the retry re-applies it from
    the restored DB value, so the still-committed total accrues exactly once.
    """
    savepoint_one = AsyncMock()
    savepoint_one.rollback = AsyncMock()
    savepoint_one.commit = AsyncMock()
    savepoint_two = AsyncMock()
    savepoint_two.rollback = AsyncMock()
    savepoint_two.commit = AsyncMock()

    session = AsyncMock()
    session.begin_nested = AsyncMock(side_effect=[savepoint_one, savepoint_two])

    # The accrual statement fails on attempt 1 (rolling the savepoint back),
    # then succeeds on attempt 2.
    accrual = AsyncMock(side_effect=[RuntimeError("accrual failed"), None])

    with (
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("modulo.core.cost_controller.finalize.increment_org_cumulative_spend", new=accrual),
    ):
        ok, reason = await _record_ledger_with_retry(
            session,
            org_id=uuid.uuid4(),
            cost_usd=Decimal("3.00"),
            team_id=None,
            run_id=uuid.uuid4(),
            run_date=date(2026, 6, 24),
            attempts=3,
            accrued_cents=300,
        )

    assert ok is True
    assert reason is None
    # Once per attempt; the first was rolled back, so only the second commits.
    assert accrual.await_count == 2
    savepoint_one.rollback.assert_awaited_once()
    savepoint_two.commit.assert_awaited_once()


async def test_record_ledger_rolls_back_savepoint_and_propagates_cancellation() -> None:
    """A ``CancelledError`` raised inside the ledger savepoint must roll the
    savepoint back and propagate untouched.

    Cancellation (task teardown / shutdown) is not a transactional write
    failure: it must never be swallowed into a retry or wrapped as a
    ``_LedgerWriteRetryError``. The savepoint is rolled back first so the
    aborted unit leaves no partial ledger row or org accrual, then the
    cancellation is re-raised for the caller to unwind.
    """
    savepoint = AsyncMock()
    savepoint.rollback = AsyncMock()
    savepoint.commit = AsyncMock()

    session = AsyncMock()
    session.begin_nested = AsyncMock(return_value=savepoint)

    with (
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await _record_ledger_with_retry(
            session,
            org_id=uuid.uuid4(),
            cost_usd=Decimal("3.00"),
            team_id=None,
            run_id=uuid.uuid4(),
            run_date=date(2026, 6, 24),
            attempts=3,
            accrued_cents=0,
        )

    savepoint.rollback.assert_awaited_once()
    savepoint.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# MAJOR 3 — a ledger-path lock abort is re-raised, never reduced-escaped
# ---------------------------------------------------------------------------


async def test_ledger_path_lock_abort_is_reraised_not_reduced_escaped() -> None:
    """A 55P03 on the LEDGER-WRITE path must propagate so the ownership layer's
    bounded whole-transaction retry owns it — NOT be converted to
    ``whole_tx_abort`` + reduced escape (which would terminalize without a
    ledger on the highest-contention path)."""
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)

    # If the reduced escape were reached, it would build a fresh session here.
    escape_factory = MagicMock(side_effect=AssertionError("reduced escape must not run on a lock abort"))

    with (
        _cost_state(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500),
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(side_effect=_SqlstateError("55P03")),
        ),
        pytest.raises(_SqlstateError),
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=escape_factory,
            claim_token=None,
        )

    escape_factory.assert_not_called()


# ---------------------------------------------------------------------------
# A NON-lock whole-tx abort reduced-escapes with a BOUNDED fresh transaction
# ---------------------------------------------------------------------------


async def test_non_lock_whole_tx_abort_reduced_escapes_with_bounded_fresh_txn() -> None:
    """A whole-tx abort that is NOT a lock abort (here ``begin_nested`` itself
    fails) is a non-retryable write failure: it must take the reduced escape,
    and that escape's FRESH transaction must have its row-lock wait bounded so
    its ``update_run_status`` cannot queue unbounded on the outer ``runs``
    FOR UPDATE (FAR-1313 / FAR-1592)."""
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)
    # The savepoint acquisition fails before the ledger write: a non-lock
    # whole-tx abort that reaches ``_ledger_block``'s except (NOT the
    # savepoint-level handler, which would swallow it as retryable).
    session.begin_nested = AsyncMock(side_effect=RuntimeError("begin_nested failed"))

    fresh = AsyncMock()
    fresh.begin = MagicMock(return_value=_acm(None))
    escape_factory = MagicMock(return_value=_acm(fresh))
    lock_bound = AsyncMock()

    with (
        _cost_state(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500),
        patch("modulo.core.cost_controller.finalize.set_rls_org", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize.set_mutation_row_lock_timeout", new=lock_bound),
        patch("modulo.core.cost_controller.finalize.guard_dual_write", new=lambda _s: _acm(None)),
        patch(
            "modulo.core.cost_controller.finalize.update_run_status",
            new=AsyncMock(return_value=None),
        ),
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=escape_factory,
            claim_token=None,
        )

    # The reduced escape ran on a fresh session AND bounded its lock waits.
    escape_factory.assert_called_once()
    lock_bound.assert_awaited_once_with(fresh)


async def test_record_ledger_skips_accrual_when_no_cents() -> None:
    """When no accrual is due (``accrued_cents`` is 0), the ledger savepoint
    must NOT attempt a cost-state increment — the accrual is strictly
    conditional on the gate handing back a non-zero cents value."""
    savepoint = AsyncMock()
    savepoint.commit = AsyncMock()
    savepoint.rollback = AsyncMock()
    session = AsyncMock()
    session.begin_nested = AsyncMock(return_value=savepoint)

    with (
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(return_value=(True, None)),
        ),
        _accrual() as accrual,
    ):
        ok, reason = await _record_ledger_with_retry(
            session,
            org_id=uuid.uuid4(),
            cost_usd=Decimal("3.00"),
            team_id=None,
            run_id=uuid.uuid4(),
            run_date=date(2026, 6, 24),
            attempts=3,
            accrued_cents=0,
        )

    assert ok is True
    assert reason is None
    accrual.assert_not_awaited()


# ---------------------------------------------------------------------------
# The legacy fallback's ledger block re-raises a lock abort, swallows the rest
# ---------------------------------------------------------------------------


async def _run_fallback(session: AsyncMock, run: MagicMock) -> None:
    await _fallback_finalize(
        session,
        run,
        run.id,
        uuid.uuid4(),
        "complete",
        None,
        None,
        _MergedSets({}, {}, {}),
        True,
        None,
        None,
    )


async def test_fallback_finalize_rereises_lock_abort_from_ledger_block() -> None:
    """A lock abort (40P01/55P03) raised by the fallback's ledger block must
    PROPAGATE to the ownership layer's bounded whole-tx retry — never be
    swallowed by the never-fail fallback envelope, which would leave the run
    un-ledgered on a contention path a retry could still cover."""
    run = _make_run()
    session = AsyncMock()
    with (
        patch(
            "modulo.core.cost_controller.finalize._apply_agent_budget_override",
            new=AsyncMock(return_value=("complete", None, None)),
        ),
        patch(
            "modulo.core.cost_controller.finalize._fallback_write",
            new=AsyncMock(return_value=Decimal("1.00")),
        ),
        patch(
            "modulo.core.cost_controller.finalize._ledger_run_date",
            new=MagicMock(return_value=date(2026, 6, 24)),
        ),
        patch(
            "modulo.core.cost_controller.finalize._ledger_block",
            new=AsyncMock(side_effect=_SqlstateError("40P01")),
        ),
        pytest.raises(_SqlstateError),
    ):
        await _run_fallback(session, run)


async def test_fallback_finalize_swallows_non_lock_ledger_error() -> None:
    """A NON-lock ledger failure inside the never-fail fallback envelope is
    logged and swallowed (the original exception must not be resurrected)."""
    run = _make_run()
    session = AsyncMock()
    ledger = AsyncMock(side_effect=RuntimeError("ledger boom"))
    with (
        patch(
            "modulo.core.cost_controller.finalize._apply_agent_budget_override",
            new=AsyncMock(return_value=("complete", None, None)),
        ),
        patch(
            "modulo.core.cost_controller.finalize._fallback_write",
            new=AsyncMock(return_value=Decimal("1.00")),
        ),
        patch(
            "modulo.core.cost_controller.finalize._ledger_run_date",
            new=MagicMock(return_value=date(2026, 6, 24)),
        ),
        patch("modulo.core.cost_controller.finalize._ledger_block", new=ledger),
    ):
        await _run_fallback(session, run)

    ledger.assert_awaited_once()


# ---------------------------------------------------------------------------
# FAR-1642 item 3 — retry-idempotent metric emission (sink semantics)
# ---------------------------------------------------------------------------


def test_metric_sink_buffers_until_flush() -> None:
    """A sink must not emit when a metric is queued — only on ``flush`` (which
    the executor calls after the committing attempt), so a rolled-back attempt
    cannot leak a counter."""
    sink = FinalizeMetricSink()
    with (
        patch("modulo.core.cost_controller.finalize.record_limit_refused") as limit_refused,
        patch("modulo.core.cost_controller.finalize.record_duplicate_terminal") as duplicate,
    ):
        sink.limit_refused("team-a")
        sink.duplicate_terminal()
        limit_refused.assert_not_called()
        duplicate.assert_not_called()

        sink.flush()
        limit_refused.assert_called_once_with("team-a")
        duplicate.assert_called_once_with()


def test_metric_sink_flush_emits_once_then_clears() -> None:
    """A second ``flush`` must not re-emit — the buffer is drained, so a retry
    that flushed once cannot double-count on a later flush."""
    sink = FinalizeMetricSink()
    with patch("modulo.core.cost_controller.finalize.record_limit_refused") as limit_refused:
        sink.limit_refused("team-a")
        sink.flush()
        sink.flush()
        limit_refused.assert_called_once_with("team-a")


def test_emit_limit_refused_defers_with_sink_and_emits_inline_without() -> None:
    """``_emit_limit_refused`` buffers when a retry-owning sink is present and
    emits inline when it is not (the cancel path / any caller without a retry)."""
    sink = FinalizeMetricSink()
    with patch("modulo.core.cost_controller.finalize.record_limit_refused") as limit_refused:
        _emit_limit_refused("team-a", sink)
        limit_refused.assert_not_called()

    with patch("modulo.core.cost_controller.finalize.record_limit_refused") as inline:
        _emit_limit_refused("team-a", None)
        inline.assert_called_once_with("team-a")


def test_emit_duplicate_terminal_defers_with_sink_and_emits_inline_without() -> None:
    """``_emit_duplicate_terminal`` mirrors ``_emit_limit_refused``'s defer/inline split."""
    sink = FinalizeMetricSink()
    with patch("modulo.core.cost_controller.finalize.record_duplicate_terminal") as duplicate:
        _emit_duplicate_terminal(sink)
        duplicate.assert_not_called()

    with patch("modulo.core.cost_controller.finalize.record_duplicate_terminal") as inline:
        _emit_duplicate_terminal(None)
        inline.assert_called_once_with()


def test_metric_sink_buffers_finalize_deferred_until_flush() -> None:
    """``finalize_deferred`` queues on the sink and only emits on ``flush`` — a
    rolled-back write-failure attempt must not count a deferred finalisation."""
    sink = FinalizeMetricSink()
    with patch("modulo.core.cost_controller.finalize.record_finalize_deferred") as deferred:
        sink.finalize_deferred("write_failure", "team-a")
        deferred.assert_not_called()

        sink.flush()
        deferred.assert_called_once_with(reason="write_failure", team="team-a")


def test_emit_finalize_deferred_defers_with_sink_and_emits_inline_without() -> None:
    """``_emit_finalize_deferred`` buffers when a retry-owning sink is present and
    emits inline when it is not (the cancel path / any caller without a retry)."""
    sink = FinalizeMetricSink()
    with patch("modulo.core.cost_controller.finalize.record_finalize_deferred") as deferred:
        _emit_finalize_deferred("write_failure", "team-a", sink)
        deferred.assert_not_called()

    with patch("modulo.core.cost_controller.finalize.record_finalize_deferred") as inline:
        _emit_finalize_deferred("write_failure", "team-a", None)
        inline.assert_called_once_with(reason="write_failure", team="team-a")


async def test_handle_ledger_write_failure_defers_with_sink_and_emits_inline_without() -> None:
    """``_handle_ledger_write_failure`` routes its ``finalize_deferred`` counter
    through the sink when one is supplied (retry-owning caller) and emits inline
    — exactly as before — when the sink is ``None`` (the no-retry path)."""
    ctx = _LedgerEscapeContext(
        run_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        status="complete",
        finalize_fields={},
        session_factory=None,
        claim_token=None,
    )
    session = MagicMock()

    with (
        patch("modulo.core.cost_controller.finalize._reduced_escape", new=AsyncMock()) as reduced_escape,
        patch("modulo.core.cost_controller.finalize.record_finalize_deferred") as deferred,
    ):
        sink = FinalizeMetricSink()
        await _handle_ledger_write_failure(session, ctx, None, "write_failure", metric_sink=sink)
        deferred.assert_not_called()
        sink.flush()
        deferred.assert_called_once_with(reason="write_failure", team="none")
        reduced_escape.assert_awaited_once()

    with (
        patch("modulo.core.cost_controller.finalize._reduced_escape", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize.record_finalize_deferred") as inline,
    ):
        await _handle_ledger_write_failure(session, ctx, None, "write_failure", metric_sink=None)
        inline.assert_called_once_with(reason="write_failure", team="none")


# ---------------------------------------------------------------------------
# FAR-1642 — pin the sink WIRE at the REAL ``_ledger_block`` call sites
# ---------------------------------------------------------------------------
#
# The tests above exercise the handlers / the sink DIRECTLY, and the executor
# tests fake ``finalize_cost`` — so none of them would notice a regression that
# drops ``metric_sink=metric_sink`` at a call site INSIDE ``_ledger_block`` or
# its handlers (the wire would silently fall back to inline emission). Each test
# below drives the REAL ``_ledger_block`` with a sink and asserts the emission
# is BUFFERED on the PASSED sink (never inline) and only fires on ``flush``.
# Drop the corresponding ``metric_sink=metric_sink`` and the inline emission
# makes the ``assert_not_called`` fail.


async def test_ledger_block_threads_sink_to_ceiling_limit_refused() -> None:
    """A ceiling refusal on the real path must buffer ``limit_refused`` into the
    PASSED sink (pins the ``_apply_spend_ceiling_gate(..., metric_sink=...)`` wire)."""
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)
    sink = FinalizeMetricSink()

    with (
        _cost_state(max_run_cost_cents=100),
        patch("modulo.core.cost_controller.finalize.record_limit_refused") as limit_refused,
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("2.00"),  # $2.00 run > $1.00 cap
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
            metric_sink=sink,
        )
        # Buffered on the PASSED sink — the wire was not cut.
        limit_refused.assert_not_called()
        sink.flush()
        limit_refused.assert_called_once_with("spend_ceiling")


async def test_ledger_block_threads_sink_to_daily_limit_refused() -> None:
    """A daily-limit refusal must buffer ``limit_refused`` into the PASSED sink
    (pins the ``_handle_limit_refused(..., metric_sink=...)`` wire)."""
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)
    sink = FinalizeMetricSink()

    with (
        _cost_state(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500),
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(return_value=(False, "daily_limit_exceeded: team")),
        ),
        _accrual(),
        patch("modulo.core.cost_controller.finalize.record_limit_refused") as limit_refused,
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
            metric_sink=sink,
        )
        limit_refused.assert_not_called()
        sink.flush()
        limit_refused.assert_called_once_with("none")


async def test_ledger_block_threads_sink_to_duplicate_terminal() -> None:
    """A duplicate-terminal guard firing must buffer ``duplicate_terminal`` into
    the PASSED sink (pins the direct ``_emit_duplicate_terminal(metric_sink)`` wire)."""
    run = _make_run()
    run.ledger_written = True  # the duplicate guard's trigger
    org = _make_org()
    session = _session_for(run, org)
    sink = FinalizeMetricSink()

    with (
        patch("modulo.core.cost_controller.finalize.record_duplicate_terminal") as duplicate,
        patch(
            "modulo.core.cost_controller.finalize._record_duplicate_terminal_event",
            new=AsyncMock(),
        ),
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("1.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
            metric_sink=sink,
        )
        duplicate.assert_not_called()
        sink.flush()
        duplicate.assert_called_once_with()


async def test_ledger_block_threads_sink_to_finalize_deferred_on_write_failure() -> None:
    """A non-lock ledger write failure must buffer ``finalize_deferred`` into the
    PASSED sink (pins the ``_handle_ledger_write_failure(..., metric_sink=...)``
    wire)."""
    run = _make_run()
    org = _make_org()
    session = _session_for(run, org)
    sink = FinalizeMetricSink()

    with (
        _cost_state(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500),
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(side_effect=RuntimeError("ledger boom")),
        ),
        patch("modulo.core.cost_controller.finalize.record_finalize_deferred") as deferred,
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
            metric_sink=sink,
        )
        deferred.assert_not_called()
        sink.flush()
        deferred.assert_called_once_with(reason="write_failure", team="none")


class _SqlstateError(Exception):
    """A stand-in DBAPI error carrying a SQLSTATE (no SQLAlchemy class)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate={sqlstate}")
        self.sqlstate = sqlstate
