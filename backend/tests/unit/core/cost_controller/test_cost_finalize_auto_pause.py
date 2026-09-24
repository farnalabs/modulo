"""Unit tests for the FAR-1183 finalize-path hooks (org auto-pause on budget).

Verifies that ``_ledger_block`` engages the org-wide trigger pause when the
toggle is on and the run crosses the ORG spend ceiling or the org DAILY spend
limit — and never for a per-run ceiling or a team-scope-only refusal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.cost_controller import finalize
from modulo.core.cost_controller import org_auto_pause
from modulo.core.cost_controller.finalize import _handle_limit_refused, _ledger_block
from modulo.db.models.daily_run_count import OrgDailyRunCount
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
    run.created_at = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    return run


def _make_org(
    *,
    max_run_cost_cents=None,
    spend_ceiling_cents=None,
    org_cumulative_spend_cents=0,
    circuit_breaker_enabled=True,
    triggers_paused=False,
) -> MagicMock:
    org = MagicMock(spec=Organisation)
    org.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    org.max_run_cost_cents = max_run_cost_cents
    org.spend_ceiling_cents = spend_ceiling_cents
    org.org_cumulative_spend_cents = org_cumulative_spend_cents
    org.daily_spend_limit = None
    org.triggers_paused = triggers_paused
    org.triggers_paused_at = None
    org.settings_json = (
        {"cost_controls": {"circuit_breaker_enabled": circuit_breaker_enabled}} if circuit_breaker_enabled else {}
    )
    return org


def _session_for(run: MagicMock, org: MagicMock) -> AsyncMock:
    """A session whose execute returns the Run (FOR UPDATE) then the Org (FOR UPDATE)."""

    def _execute(stmt):
        text = str(stmt)
        result = MagicMock()
        if "org_daily_run_counts" in text:
            result.scalar_one_or_none = MagicMock(return_value=None)
        elif "organisations" in text:
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


async def test_org_ceiling_exceeded_with_toggle_on_auto_pauses() -> None:
    run = _make_run()
    org = _make_org(spend_ceiling_cents=100, org_cumulative_spend_cents=100)  # $1.00 ceiling, consumed
    session = _session_for(run, org)

    with patch.object(finalize, "_auto_pause_org_triggers", new=AsyncMock()) as auto_pause:
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("2.00"),
            owner_team_id=None,
            run_date=date(2026, 9, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    assert run.status == "cost_ceiling_exceeded"
    auto_pause.assert_awaited_once()
    assert auto_pause.await_args.kwargs["reason"] == "spend_ceiling"
    assert auto_pause.await_args.kwargs["limit_cents"] == 100
    assert auto_pause.await_args.kwargs["spend_cents"] == 300


async def test_toggle_off_ceiling_crossing_does_not_pause() -> None:
    """With the toggle off the REAL auto-pause gate is a strict no-op (no writes)."""
    run = _make_run()
    org = _make_org(spend_ceiling_cents=100, org_cumulative_spend_cents=100, circuit_breaker_enabled=False)
    session = _session_for(run, org)

    with (
        patch.object(org_auto_pause, "append_audit_event", new=AsyncMock()) as audit,
        patch.object(org_auto_pause, "_notify_admins", new=AsyncMock()) as notify,
    ):
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("2.00"),
            owner_team_id=None,
            run_date=date(2026, 9, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    assert run.status == "cost_ceiling_exceeded"
    assert org.triggers_paused is False
    audit.assert_not_awaited()
    notify.assert_not_awaited()


async def test_run_ceiling_only_does_not_pause() -> None:
    run = _make_run()
    org = _make_org(max_run_cost_cents=100, org_cumulative_spend_cents=0)  # per-run cap only
    session = _session_for(run, org)

    with patch.object(finalize, "_auto_pause_org_triggers", new=AsyncMock()) as auto_pause:
        await _ledger_block(
            session,
            run_id=run.id,
            org_id=org.id,
            status="complete",
            total=Decimal("2.00"),
            owner_team_id=None,
            run_date=date(2026, 9, 24),
            finalize_fields={},
            session_factory=None,
            claim_token=None,
        )

    assert run.status == "cost_ceiling_exceeded"
    auto_pause.assert_not_awaited()


async def test_daily_limit_org_crossing_auto_pauses() -> None:
    """An org-scope ``daily_limit_exceeded`` refusal with the toggle on engages the pause."""
    run = _make_run()
    org = _make_org()
    org.daily_spend_limit = Decimal("10.00")  # $10.00 daily limit
    locked = run  # the FOR UPDATE locked run

    def _execute(stmt):
        text = str(stmt)
        result = MagicMock()
        if "organisations" in text:
            result.scalar_one_or_none = MagicMock(return_value=org)
        elif "org_daily_run_counts" in text:
            result.scalar_one_or_none = MagicMock(return_value=Decimal("9.00"))
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.flush = AsyncMock()

    with patch.object(finalize, "_auto_pause_org_triggers", new=AsyncMock()) as auto_pause:
        await _handle_limit_refused(
            session,
            locked,
            run.id,
            owner_team_id=None,
            org_id=org.id,
            reason="daily_limit_exceeded: organisation",
            total=Decimal("2.00"),
        )

    assert locked.ledger_refused_at is not None
    auto_pause.assert_awaited_once()
    assert auto_pause.await_args.kwargs["reason"] == "daily_spend_limit"
    assert auto_pause.await_args.kwargs["spend_cents"] == 1100
    assert auto_pause.await_args.kwargs["limit_cents"] == 1000


async def test_daily_limit_team_scope_only_does_not_pause() -> None:
    run = _make_run()
    org = _make_org()
    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    with patch.object(finalize, "_auto_pause_org_triggers", new=AsyncMock()) as auto_pause:
        await _handle_limit_refused(
            session,
            run,
            run.id,
            owner_team_id=uuid.uuid4(),
            org_id=org.id,
            reason="daily_limit_exceeded: team",
            total=Decimal("1.00"),
        )

    assert run.ledger_refused_at is not None
    auto_pause.assert_not_awaited()
