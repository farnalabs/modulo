"""Unit tests for the FAR-391 hard spend-ceiling enforcement in the terminal ledger block.

Verifies that ``_ledger_block`` refuses the ledger (and terminalizes the run as
``cost_ceiling_exceeded``) when the per-run or per-org ceiling is breached, and
that on success the org's consumed total is incremented. DB is fully mocked.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

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


async def test_within_ceilings_increments_org_cumulative() -> None:
    run = _make_run()
    org = _make_org(spend_ceiling_cents=10_000, org_cumulative_spend_cents=500)  # $100 ceiling, $5 consumed
    session = _session_for(run, org)

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

    # No refusal — the gate increments the org's consumed total by 300 cents
    # (the daily-ledger write that follows is out of scope for this unit test).
    assert run.ledger_refused_at is None
    assert org.org_cumulative_spend_cents == 800
