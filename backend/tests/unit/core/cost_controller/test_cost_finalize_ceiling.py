"""Unit tests for the FAR-391 hard spend-ceiling enforcement in the terminal ledger block.

Verifies that ``_ledger_block`` refuses the ledger (and terminalizes the run as
``cost_ceiling_exceeded``) when the per-run or per-org ceiling is breached, and
that on success the org's consumed total is incremented. DB is fully mocked.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.cost_controller.finalize import _ledger_block
from modulo.core.spend_ceiling import ORG_CEILING_EXCEEDED, RUN_CEILING_EXCEEDED
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run


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

    skip_ledger, accrual_org = await _apply_spend_ceiling_gate(
        session,
        run,
        org_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        total=Decimal("1.00"),
        run_id=run.id,
    )

    assert skip_ledger is False
    assert accrual_org is None


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
