"""Unit tests for the overdue-HITL-claim warning system.

Covers ``get_overdue_claims``: the input validation gates, warning vs
escalation classification, the age floor, null-``claimed_at`` filtering, and
query-failure tolerance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.hitl_manager.overdue_warning import (
    DEFAULT_ESCALATION_HOURS,
    DEFAULT_WARNING_HOURS,
    get_overdue_claims,
)


def _claim(*, claimed_at: datetime, gate_id: str = "review") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id=gate_id,
        claimed_at=claimed_at,
    )


def _make_session(*claims: SimpleNamespace) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(claims)
    session.execute = AsyncMock(return_value=result)
    return session


class TestOverdueClaimsValidation:
    @pytest.mark.parametrize("warning_hours", [-1, -10])
    async def test_rejects_negative_warning_hours(self, warning_hours: int) -> None:
        with pytest.raises(ValueError, match="warning_hours"):
            await get_overdue_claims(AsyncMock(), uuid.uuid4(), warning_hours=warning_hours)

    @pytest.mark.parametrize("escalation_hours", [-1, -10])
    async def test_rejects_negative_escalation_hours(self, escalation_hours: int) -> None:
        with pytest.raises(ValueError, match="escalation_hours"):
            await get_overdue_claims(AsyncMock(), uuid.uuid4(), escalation_hours=escalation_hours)

    async def test_rejects_escalation_not_above_warning(self) -> None:
        with pytest.raises(ValueError, match="escalation_hours"):
            await get_overdue_claims(AsyncMock(), uuid.uuid4(), warning_hours=24, escalation_hours=24)

    async def test_rejects_escalation_below_warning(self) -> None:
        with pytest.raises(ValueError, match="escalation_hours"):
            await get_overdue_claims(AsyncMock(), uuid.uuid4(), warning_hours=24, escalation_hours=1)


class TestGetOverdueClaims:
    async def test_returns_empty_when_no_claims(self) -> None:
        session = _make_session()
        assert await get_overdue_claims(session, uuid.uuid4()) == []

    async def test_classifies_old_claim_as_warning(self) -> None:
        now = datetime.now(UTC)
        old_claim = _claim(claimed_at=now - timedelta(hours=DEFAULT_WARNING_HOURS + 2), gate_id="gate-1")
        session = _make_session(old_claim)

        result = await get_overdue_claims(session, uuid.uuid4())

        assert len(result) == 1
        entry = result[0]
        assert entry["node_id"] == "gate-1"
        assert entry["status"] == "warning"
        assert entry["age_hours"] == round((now - old_claim.claimed_at).total_seconds() / 3600, 1)

    async def test_classifies_very_old_claim_as_escalated(self) -> None:
        now = datetime.now(UTC)
        ancient_claim = _claim(claimed_at=now - timedelta(hours=DEFAULT_ESCALATION_HOURS + 2))
        session = _make_session(ancient_claim)

        result = await get_overdue_claims(session, uuid.uuid4())

        assert len(result) == 1
        assert result[0]["status"] == "escalated"

    async def test_mixes_warning_and_escalated_in_age_order(self) -> None:
        now = datetime.now(UTC)
        warning_claim = _claim(claimed_at=now - timedelta(hours=DEFAULT_WARNING_HOURS + 2), gate_id="recent")
        escalated_claim = _claim(
            claimed_at=now - timedelta(hours=DEFAULT_ESCALATION_HOURS + 2),
            gate_id="ancient",
        )
        session = _make_session(escalated_claim, warning_claim)

        result = await get_overdue_claims(session, uuid.uuid4())

        assert len(result) == 2
        statuses = {entry["node_id"]: entry["status"] for entry in result}
        assert statuses == {"recent": "warning", "ancient": "escalated"}

    async def test_age_hours_floor_at_zero_for_future_claimed_at(self) -> None:
        now = datetime.now(UTC)
        future_claim = _claim(claimed_at=now + timedelta(hours=1))
        session = _make_session(future_claim)

        result = await get_overdue_claims(session, uuid.uuid4())

        assert len(result) == 1
        assert result[0]["age_hours"] == 0.0
        assert result[0]["status"] == "warning"

    async def test_filters_claims_with_null_claimed_at(self) -> None:
        null_claim = _claim(claimed_at=datetime.now(UTC) - timedelta(hours=100))
        null_claim.claimed_at = None
        session = _make_session(null_claim)

        result = await get_overdue_claims(session, uuid.uuid4())

        assert result == []

    async def test_query_filters_undecided_claimed_claims_for_org(self) -> None:
        org_id = uuid.uuid4()
        session = _make_session()
        now = datetime.now(UTC)

        await get_overdue_claims(session, org_id, warning_hours=5, escalation_hours=10)

        stmt = session.execute.await_args.args[0]
        params = stmt.compile().params
        assert params["organisation_id_1"] == org_id
        cutoff = params["claimed_at_1"]
        assert abs((cutoff - (now - timedelta(hours=5))).total_seconds()) < 5
        assert "account_id" in str(stmt)
        assert "decision" in str(stmt)

    async def test_custom_thresholds_are_respected(self) -> None:
        now = datetime.now(UTC)
        claim = _claim(claimed_at=now - timedelta(hours=6), gate_id="six-hours")
        session = _make_session(claim)

        result = await get_overdue_claims(session, uuid.uuid4(), warning_hours=2, escalation_hours=8)

        assert len(result) == 1
        assert result[0]["status"] == "warning"

    async def test_query_failure_returns_empty(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))

        result = await get_overdue_claims(session, uuid.uuid4())

        assert result == []

    async def test_default_thresholds_documented(self) -> None:
        assert DEFAULT_WARNING_HOURS == 4
        assert DEFAULT_ESCALATION_HOURS == 24
