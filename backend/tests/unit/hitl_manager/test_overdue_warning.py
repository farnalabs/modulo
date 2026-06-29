"""Unit tests for overdue_warning.get_overdue_claims."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from modulo.core.hitl_manager.overdue_warning import get_overdue_claims
from modulo.db.models.hitl_claim import HitlClaim

_ORG = uuid.uuid4()


def _claim(
    *,
    created_at: datetime | None = None,
    run_id: uuid.UUID | None = None,
    gate_id: str = "review-step",
) -> MagicMock:
    g = MagicMock(spec=HitlClaim)
    g.id = uuid.uuid4()
    g.run_id = run_id or uuid.uuid4()
    g.gate_id = gate_id
    g.organisation_id = _ORG
    g.created_at = created_at or datetime.now(UTC)
    g.pipeline_id = uuid.uuid4()
    g.decision = None
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
    mock_old = _claim(created_at=now - timedelta(hours=10))

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
    mock_oldish = _claim(created_at=now - timedelta(hours=6))

    session = _mock_session([mock_oldish])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["status"] == "warning"


async def test_escalates_claims_older_than_escalation_threshold() -> None:
    now = datetime.now(UTC)
    mock_warning = _claim(created_at=now - timedelta(hours=8))
    mock_escalated = _claim(created_at=now - timedelta(hours=48))

    session = _mock_session([mock_warning, mock_escalated])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 2
    statuses = {r["id"]: r["status"] for r in result}
    assert statuses[str(mock_warning.id)] == "warning"
    assert statuses[str(mock_escalated.id)] == "escalated"


async def test_ignores_decided_claims() -> None:
    now = datetime.now(UTC)
    mock_pending = _claim(created_at=now - timedelta(hours=10))

    session = _mock_session([mock_pending])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert len(result) == 1
    assert result[0]["id"] == str(mock_pending.id)


async def test_returns_empty_when_no_overdue_claims() -> None:
    session = _mock_session([])
    result = await get_overdue_claims(session, _ORG, warning_hours=4)

    assert result == []
