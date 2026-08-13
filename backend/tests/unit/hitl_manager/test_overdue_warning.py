"""Unit tests for overdue_warning.get_overdue_claims."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.hitl_manager.overdue_warning import (
    DEFAULT_ESCALATION_HOURS,
    DEFAULT_WARNING_HOURS,
    get_overdue_claims,
)
from modulo.db.models.hitl_claim import HitlClaim

_ORG = uuid.uuid4()


def _claim(
    *,
    claimed_at: datetime | None = None,
    run_id: uuid.UUID | None = None,
    gate_id: str = "review-step",
    account_id: uuid.UUID | None = None,
    decision: str | None = None,
) -> MagicMock:
    g = MagicMock(spec=HitlClaim)
    g.id = uuid.uuid4()
    g.run_id = run_id or uuid.uuid4()
    g.gate_id = gate_id
    g.organisation_id = _ORG
    g.claimed_at = claimed_at or (datetime.now(UTC) if account_id else None)
    g.pipeline_id = uuid.uuid4()
    g.decision = decision
    g.account_id = account_id
    return g


def _mock_session(claims: list) -> AsyncMock:
    """Build an AsyncMock session where execute → scalars → all → claims."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = claims

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)
    return session


async def test_returns_claims_older_than_warning_threshold() -> None:
    now = datetime.now(UTC)
    mock_old = _claim(claimed_at=now - timedelta(hours=10), account_id=uuid.uuid4())

    session = _mock_session([mock_old])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["id"] == str(mock_old.id)
    assert result[0]["pipeline_run_id"] == str(mock_old.run_id)
    assert result[0]["node_id"] == mock_old.gate_id
    assert result[0]["age_hours"] >= 9.9
    assert result[0]["status"] == "warning"


async def test_respects_warning_hours_threshold() -> None:
    now = datetime.now(UTC)
    mock_oldish = _claim(claimed_at=now - timedelta(hours=6), account_id=uuid.uuid4())

    session = _mock_session([mock_oldish])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["status"] == "warning"


async def test_escalates_claims_older_than_escalation_threshold() -> None:
    now = datetime.now(UTC)
    mock_warning = _claim(claimed_at=now - timedelta(hours=8), account_id=uuid.uuid4())
    mock_escalated = _claim(claimed_at=now - timedelta(hours=48), account_id=uuid.uuid4())

    session = _mock_session([mock_warning, mock_escalated])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 2
    statuses = {r["id"]: r["status"] for r in result}
    assert statuses[str(mock_warning.id)] == "warning"
    assert statuses[str(mock_escalated.id)] == "escalated"


async def test_ignores_decided_claims() -> None:
    now = datetime.now(UTC)
    mock_pending = _claim(claimed_at=now - timedelta(hours=10), account_id=uuid.uuid4())
    mock_decided = _claim(
        claimed_at=now - timedelta(hours=10),
        account_id=uuid.uuid4(),
        decision="approved",
    )

    # The DB query includes decision IS NULL in its WHERE clause
    # (see overdue_warning.py line 54). The mock simulates that
    # by only including pending claims.
    session = _mock_session([mock_pending])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["id"] == str(mock_pending.id)
    assert result[0]["id"] != str(mock_decided.id)


async def test_returns_empty_when_no_overdue_claims() -> None:
    session = _mock_session([])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert result == []


async def test_rejects_negative_warning_hours() -> None:
    session = AsyncMock()
    with pytest.raises(ValueError, match="warning_hours must be non-negative"):
        await get_overdue_claims(session, _ORG, warning_hours=-1)


async def test_rejects_negative_escalation_hours() -> None:
    session = AsyncMock()
    with pytest.raises(ValueError, match="escalation_hours must be non-negative"):
        await get_overdue_claims(session, _ORG, escalation_hours=-5)


async def test_rejects_escalation_hours_not_greater_than_warning() -> None:
    session = AsyncMock()
    with pytest.raises(ValueError, match=r"escalation_hours .* must exceed warning_hours"):
        await get_overdue_claims(session, _ORG, warning_hours=6, escalation_hours=6)


async def test_returns_empty_when_query_fails() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert result == []


async def test_age_hours_floor_at_zero_for_future_claimed_at() -> None:
    """A future claimed_at (clock skew) must report age_hours 0, not negative."""
    now = datetime.now(UTC)
    future_claim = _claim(claimed_at=now + timedelta(hours=1), account_id=uuid.uuid4())

    session = _mock_session([future_claim])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["age_hours"] == 0.0
    assert result[0]["status"] == "warning"


async def test_filters_claims_with_null_claimed_at() -> None:
    """Claims whose claimed_at is NULL are skipped by the query and never reported."""
    null_claim = _claim(claimed_at=datetime.now(UTC) - timedelta(hours=100), account_id=uuid.uuid4())
    null_claim.claimed_at = None

    session = _mock_session([null_claim])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert result == []


async def test_query_filters_undecided_claimed_claims_for_org() -> None:
    """The SQL predicate scopes to org and only undecided, claimed, non-null claimed_at rows."""
    org_id = uuid.uuid4()
    session = _mock_session([])
    now = datetime.now(UTC)

    await get_overdue_claims(session, org_id, warning_hours=5, escalation_hours=10)

    stmt = session.execute.await_args.args[0]
    params = stmt.compile().params
    assert params["organisation_id_1"] == org_id
    cutoff = params["claimed_at_1"]
    assert abs((cutoff - (now - timedelta(hours=5))).total_seconds()) < 5
    assert "account_id" in str(stmt)
    assert "decision" in str(stmt)


async def test_default_thresholds_documented() -> None:
    """The shipped warning/escalation thresholds are the documented defaults."""
    assert DEFAULT_WARNING_HOURS == 4
    assert DEFAULT_ESCALATION_HOURS == 24
