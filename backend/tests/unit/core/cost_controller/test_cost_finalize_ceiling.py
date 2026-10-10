"""Unit tests for the FAR-391 hard spend-ceiling enforcement in the terminal ledger block.

Verifies that ``_ledger_block`` refuses the ledger (and terminalizes the run as
``cost_ceiling_exceeded``) when the per-run or per-org ceiling is breached, and
that on success the org's consumed total is incremented. DB is fully mocked.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller.finalize import (
    _fallback_finalize,
    _ledger_block,
    _record_ledger_with_retry,
)
from modulo.core.spend_ceiling import ORG_CEILING_EXCEEDED, RUN_CEILING_EXCEEDED
from modulo.db.models.organisation import Organisation
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


def _make_org(*, max_run_cost_cents=None, spend_ceiling_cents=None, org_cumulative_spend_cents=0) -> MagicMock:
    org = MagicMock(spec=Organisation)
    org.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    org.max_run_cost_cents = max_run_cost_cents
    org.spend_ceiling_cents = spend_ceiling_cents
    org.org_cumulative_spend_cents = org_cumulative_spend_cents
    return org


async def test_spend_ceiling_gate_locks_org_for_no_key_update() -> None:
    """FAR-1624: the ceiling gate must take ``FOR NO KEY UPDATE`` (not the
    stronger ``FOR UPDATE``) on the org row.

    The finalisation transaction already holds a foreign-key ``FOR KEY SHARE``
    on the org tuple (from rows it inserted/updated that reference the org), so
    requesting ``FOR UPDATE`` deadlocked up to 8 concurrent finalisations
    (SQLSTATE 40P01). ``FOR NO KEY UPDATE`` is compatible with ``FOR KEY
    SHARE`` while still conflicting with itself, so concurrent spend accrual
    stays serialised.
    """
    from sqlalchemy.dialects import postgresql

    from modulo.core.cost_controller.finalize import _apply_spend_ceiling_gate

    org = _make_org(spend_ceiling_cents=None, org_cumulative_spend_cents=0)
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
    skip_ledger, _accrual_org, _accrual_cents = await _apply_spend_ceiling_gate(
        session,
        run,
        org_id=org.id,
        total=Decimal("1.00"),
        run_id=run.id,
    )

    assert skip_ledger is False
    assert len(captured) == 1
    sql = str(captured[0].compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    assert "FOR NO KEY UPDATE" in sql
    assert "FOR UPDATE" not in sql


def _session_for(run: MagicMock, org: MagicMock) -> AsyncMock:
    """A session whose execute returns the Run (FOR UPDATE) then the Org (FOR UPDATE)."""

    def _execute(stmt):
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
    org = _make_org(spend_ceiling_cents=100, org_cumulative_spend_cents=100)  # $1.00 ceiling, already consumed
    session = _session_for(run, org)

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
    # Org cumulative must NOT be incremented on refusal.
    assert org.org_cumulative_spend_cents == 100


async def test_run_ceiling_exceeded_refuses_ledger() -> None:
    run = _make_run()
    org = _make_org(max_run_cost_cents=100, org_cumulative_spend_cents=0)  # $1.00 per-run cap
    session = _session_for(run, org)

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
    org = _make_org(spend_ceiling_cents=100, org_cumulative_spend_cents=100)
    session = _session_for(run, org)

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
    assert org.org_cumulative_spend_cents == 100


async def test_org_row_missing_skips_gate_and_ledger_write() -> None:
    """FAR-1025: an org row that vanished is treated as no ceiling — the gate
    returns ``(False, None)`` so the ledger write proceeds without an accrual."""
    from modulo.core.cost_controller.finalize import _apply_spend_ceiling_gate

    run = _make_run()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    skip_ledger, accrual_org, accrual_cents = await _apply_spend_ceiling_gate(
        session,
        run,
        org_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        total=Decimal("1.00"),
        run_id=run.id,
    )

    assert skip_ledger is False
    assert accrual_org is None
    assert accrual_cents == 0


async def test_within_ceilings_increments_org_cumulative() -> None:
    run = _make_run()
    org = _make_org(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500)  # $100 ceiling, $5 consumed
    session = _session_for(run, org)

    with (
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(return_value=(True, None)),
        ),
        # The best-effort circuit-breaker check is out of scope here and would
        # otherwise issue real queries against the mock session.
        patch("modulo.core.cost_controller.finalize._check_circuit_breaker", new=AsyncMock()),
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("3.00"),  # $3.00 run -> cumulative $8.00
            owner_team_id=None,
            run_date=date(2026, 6, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    # Only a SUCCESSFUL ledger write accrues the org's consumed total (the
    # accrual rides the ledger savepoint); $5.00 + $3.00 = $8.00.
    assert run.ledger_refused_at is None
    assert run.ledger_written is True
    assert org.org_cumulative_spend_cents == 800


async def test_ledger_write_failure_then_refinalize_does_not_double_accrue() -> None:
    """Money-correctness regression: a contained (non-abort) ledger-write failure
    must NOT commit the org accrual, so a later re-finalisation of the SAME run
    accrues exactly once (the ``ledger_written`` guard does not cover the
    accrual)."""
    run = _make_run()
    org = _make_org(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500)
    session = _session_for(run, org)

    # First finalisation: every ledger-write attempt fails (non-abort). The
    # savepoint rolls back, so the accrual must NOT commit.
    with patch(
        "modulo.core.cost_controller.finalize.check_and_record_spend",
        new=AsyncMock(side_effect=RuntimeError("ledger write failed")),
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

    assert org.org_cumulative_spend_cents == 500  # no accrual on write failure
    assert run.ledger_written is False  # left re-finalisable by the reduced escape

    # Re-finalisation: the ledger write now succeeds -> accrue exactly once.
    with (
        patch(
            "modulo.core.cost_controller.finalize.check_and_record_spend",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("modulo.core.cost_controller.finalize._check_circuit_breaker", new=AsyncMock()),
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

    assert org.org_cumulative_spend_cents == 800  # 500 + 300, NOT 500 + 300 + 300
    assert run.ledger_written is True


# ---------------------------------------------------------------------------
# MAJOR 2 — a retry must never re-read a savepoint-expired accrual attribute
# ---------------------------------------------------------------------------


class _SqlstateError(Exception):
    """A stand-in DBAPI error carrying a SQLSTATE (no SQLAlchemy class)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate={sqlstate}")
        self.sqlstate = sqlstate


class _ExpiringOrg:
    """An org whose cumulative attribute read FAILS once marked expired.

    Mirrors SQLAlchemy's savepoint-rollback behaviour: a rolled-back savepoint
    EXPIRES the Organisation's loaded state, so a subsequent attribute read
    triggers a lazy refresh — which, outside an async greenlet, raises
    ``MissingGreenlet``. Reading the base BEFORE the retry loop must never
    touch the attribute again.
    """

    def __init__(self, base: int) -> None:
        self._value = base
        self.reads = 0
        self.expired = False

    @property
    def org_cumulative_spend_cents(self) -> int:
        self.reads += 1
        if self.expired:
            from sqlalchemy.exc import MissingGreenlet

            raise MissingGreenlet("cannot lazy-load an expired attribute outside a greenlet")
        return self._value

    @org_cumulative_spend_cents.setter
    def org_cumulative_spend_cents(self, value: int) -> None:
        self._value = value

    def stored_value(self) -> int:
        """The raw stored value — reads WITHOUT incrementing ``reads`` or
        raising once expired (for the test's own assertions)."""
        return self._value


async def test_accrual_retry_after_flush_failure_never_rereads_expired_org() -> None:
    """A failed accrual flush expires the org; the retry must not re-read it.

    Regression: reading ``org_cumulative_spend_cents`` on every attempt raised
    ``MissingGreenlet`` after the savepoint rollback expired the object — a
    non-abort error that exhausted the retries and lost the ledger. The base is
    captured once before the loop, so the retry assigns the absolute value and
    accrues exactly once.
    """
    org = _ExpiringOrg(base=500)
    first_savepoint = AsyncMock()
    first_savepoint.rollback = AsyncMock(side_effect=lambda: setattr(org, "expired", True))
    first_savepoint.commit = AsyncMock()
    second_savepoint = AsyncMock()
    second_savepoint.rollback = AsyncMock()
    second_savepoint.commit = AsyncMock()

    session = AsyncMock()
    session.begin_nested = AsyncMock(side_effect=[first_savepoint, second_savepoint])
    # The accrual flush fails on attempt 1 (rolling the savepoint back and
    # expiring the org), then succeeds on attempt 2.
    session.flush = AsyncMock(side_effect=[RuntimeError("accrual flush failed"), None])

    with patch(
        "modulo.core.cost_controller.finalize.check_and_record_spend",
        new=AsyncMock(return_value=(True, None)),
    ):
        ok, reason = await _record_ledger_with_retry(
            session,
            org_id=uuid.uuid4(),
            cost_usd=Decimal("3.00"),
            team_id=None,
            run_id=uuid.uuid4(),
            run_date=date(2026, 6, 24),
            attempts=3,
            accrued_org=org,  # type: ignore[arg-type]
            accrued_cents=300,
        )

    assert ok is True
    assert reason is None
    assert org.reads == 1, "the base must be read exactly once, before the retry loop"
    assert org.stored_value() == 800  # exactly one accrual of 300, no re-read
    assert org.expired is True  # the rollback really did expire the object


# ---------------------------------------------------------------------------
# MAJOR 3 — a ledger-path lock abort is re-raised, never reduced-escaped
# ---------------------------------------------------------------------------


async def test_ledger_path_lock_abort_is_reraised_not_reduced_escaped() -> None:
    """A 55P03 on the LEDGER-WRITE path must propagate so the ownership layer's
    bounded whole-transaction retry owns it — NOT be converted to
    ``whole_tx_abort`` + reduced escape (which would terminalize without a
    ledger on the highest-contention path)."""
    run = _make_run()
    org = _make_org(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500)
    session = _session_for(run, org)

    # If the reduced escape were reached, it would build a fresh session here.
    escape_factory = MagicMock(side_effect=AssertionError("reduced escape must not run on a lock abort"))

    with (
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
    org = _make_org(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500)
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


async def test_record_ledger_skips_accrual_when_no_org() -> None:
    """When no accrual is due (``accrued_org`` is None), the ledger savepoint
    must NOT attempt an org increment/flush — the accrual is strictly
    conditional on the gate handing back a locked org row."""
    savepoint = AsyncMock()
    savepoint.commit = AsyncMock()
    savepoint.rollback = AsyncMock()
    session = AsyncMock()
    session.begin_nested = AsyncMock(return_value=savepoint)

    with patch(
        "modulo.core.cost_controller.finalize.check_and_record_spend",
        new=AsyncMock(return_value=(True, None)),
    ):
        ok, reason = await _record_ledger_with_retry(
            session,
            org_id=uuid.uuid4(),
            cost_usd=Decimal("3.00"),
            team_id=None,
            run_id=uuid.uuid4(),
            run_date=date(2026, 6, 24),
            attempts=3,
            accrued_org=None,
            accrued_cents=0,
        )

    assert ok is True
    assert reason is None
    session.flush.assert_not_awaited()


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
        {},
        {},
        {},
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
