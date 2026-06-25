"""Unit tests for modulo.core.cost_controller.

All DB interaction is mocked; we verify logic, limit enforcement,
and correct SQL filtering.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller import (
    check_and_record_spend,
    get_cost_report,
    get_or_create_daily_count,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TODAY = date(2026, 6, 24)


@pytest.fixture()
def mock_session() -> AsyncMock:
    return AsyncMock()


def _make_daily_count_row(**kw: object) -> MagicMock:
    row = MagicMock()
    row.id = kw.get("id", uuid.uuid4())
    row.organisation_id = kw.get("organisation_id", _ORG_ID)
    row.team_id = kw.get("team_id", None)
    row.run_date = kw.get("run_date", _TODAY)
    row.run_count = kw.get("run_count", 0)
    row.total_spend_usd = kw.get("total_spend_usd", Decimal("0"))
    return row


# ---------------------------------------------------------------------------
# get_or_create_daily_count
# ---------------------------------------------------------------------------


class TestGetOrCreateDailyCount:
    async def test_returns_existing_row(self, mock_session: AsyncMock) -> None:
        existing = _make_daily_count_row(run_count=5, total_spend_usd=Decimal("12.50"))
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        )

        result = await get_or_create_daily_count(
            mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None
        )

        assert result is existing
        assert result.run_count == 5
        assert result.total_spend_usd == Decimal("12.50")

    async def test_creates_new_row_when_missing(self, mock_session: AsyncMock) -> None:
        first = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        lock = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[first, lock])

        result = await get_or_create_daily_count(
            mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None
        )

        assert result.run_count == 0
        assert result.total_spend_usd == Decimal("0")
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()

    async def test_creates_new_team_row(self, mock_session: AsyncMock) -> None:
        first = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        lock = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[first, lock])

        result = await get_or_create_daily_count(
            mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=_TEAM_ID
        )

        assert result.team_id == _TEAM_ID
        mock_session.add.assert_called_once()

    async def test_uses_select_for_update(self, mock_session: AsyncMock) -> None:
        existing = _make_daily_count_row()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
        )

        await get_or_create_daily_count(
            mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None
        )

        call = mock_session.execute.call_args[0][0]
        assert "FOR UPDATE" in str(call).upper()


# ---------------------------------------------------------------------------
# check_and_record_spend
# ---------------------------------------------------------------------------


class TestCheckAndRecordSpend:
    _FROZEN = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)

    @pytest.fixture(autouse=True)
    def _freeze_datetime(self) -> Iterator[None]:
        with patch("modulo.core.cost_controller.datetime") as mock_dt:
            mock_dt.now.return_value = self._FROZEN
            mock_dt.UTC = UTC
            mock_dt.date = date
            mock_dt.timedelta = timedelta
            mock_dt.datetime = datetime
            yield

    async def test_approves_spend_without_limit(self, mock_session: AsyncMock) -> None:
        org_count = _make_daily_count_row(run_count=2, total_spend_usd=Decimal("10"))
        org_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=org_count)),
            org_limit_result,
        ])

        approved, reason = await check_and_record_spend(
            mock_session, org_id=_ORG_ID, cost_usd=Decimal("5.50"), team_id=None
        )

        assert approved is True
        assert reason is None
        assert org_count.total_spend_usd == Decimal("15.50")
        assert org_count.run_count == 3
        mock_session.flush.assert_awaited_once()

    async def test_rejects_spend_over_org_limit(
        self, mock_session: AsyncMock
    ) -> None:
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal("90"))
        org_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=Decimal("100")))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=org_count)),
            org_limit_result,
        ])

        approved, reason = await check_and_record_spend(
            mock_session, org_id=_ORG_ID, cost_usd=Decimal("20"), team_id=None
        )

        assert approved is False
        assert "organisation" in (reason or "").lower()
        assert org_count.run_count == 0

    async def test_approves_spend_at_exact_limit(self, mock_session: AsyncMock) -> None:
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal("90"))
        org_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=Decimal("100")))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=org_count)),
            org_limit_result,
        ])

        approved, reason = await check_and_record_spend(
            mock_session, org_id=_ORG_ID, cost_usd=Decimal("10"), team_id=None
        )

        assert approved is True
        assert reason is None
        assert org_count.total_spend_usd == Decimal("100")

    async def test_approves_with_team_under_both_limits(
        self, mock_session: AsyncMock
    ) -> None:
        org_count = _make_daily_count_row(run_count=1, total_spend_usd=Decimal("30"))
        org_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=Decimal("200")))
        team_count = _make_daily_count_row(
            team_id=_TEAM_ID, run_count=0, total_spend_usd=Decimal("10")
        )
        team_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=Decimal("50")))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=org_count)),
            org_limit_result,
            MagicMock(scalar_one_or_none=MagicMock(return_value=team_count)),
            team_limit_result,
        ])

        approved, reason = await check_and_record_spend(
            mock_session, org_id=_ORG_ID, cost_usd=Decimal("5"), team_id=_TEAM_ID
        )

        assert approved is True
        assert reason is None
        assert org_count.total_spend_usd == Decimal("35")
        assert org_count.run_count == 2
        assert team_count.total_spend_usd == Decimal("15")
        assert team_count.run_count == 1

    async def test_rejects_spend_over_team_limit(
        self, mock_session: AsyncMock
    ) -> None:
        org_count = _make_daily_count_row(run_count=1, total_spend_usd=Decimal("5"))
        org_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=Decimal("200")))
        team_count = _make_daily_count_row(
            team_id=_TEAM_ID, run_count=2, total_spend_usd=Decimal("45")
        )
        team_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=Decimal("50")))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=org_count)),
            org_limit_result,
            MagicMock(scalar_one_or_none=MagicMock(return_value=team_count)),
            team_limit_result,
        ])

        approved, reason = await check_and_record_spend(
            mock_session, org_id=_ORG_ID, cost_usd=Decimal("10"), team_id=_TEAM_ID
        )

        assert approved is False
        assert "team" in (reason or "").lower()
        # Neither org nor team counts are modified when team limit is exceeded.
        assert org_count.run_count == 1
        assert team_count.run_count == 2

    async def test_approves_when_both_limits_none(
        self, mock_session: AsyncMock
    ) -> None:
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal("0"))
        org_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        team_count = _make_daily_count_row(
            team_id=_TEAM_ID, run_count=0, total_spend_usd=Decimal("0")
        )
        team_limit_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=org_count)),
            org_limit_result,
            MagicMock(scalar_one_or_none=MagicMock(return_value=team_count)),
            team_limit_result,
        ])

        approved, reason = await check_and_record_spend(
            mock_session, org_id=_ORG_ID, cost_usd=Decimal("99999"), team_id=_TEAM_ID
        )

        assert approved is True
        assert reason is None


# ---------------------------------------------------------------------------
# get_cost_report
# ---------------------------------------------------------------------------


class TestGetCostReport:
    _FROZEN = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)

    @pytest.fixture(autouse=True)
    def _freeze_datetime(self) -> Iterator[None]:
        with patch("modulo.core.cost_controller.datetime") as mock_dt:
            mock_dt.now.return_value = self._FROZEN
            mock_dt.UTC = UTC
            mock_dt.date = date
            mock_dt.timedelta = timedelta
            mock_dt.datetime = datetime
            yield

    def _make_team_result(self, team: MagicMock) -> MagicMock:
        scalars_mock = MagicMock(all=MagicMock(return_value=[team]))
        return MagicMock(scalars=MagicMock(return_value=scalars_mock))

    async def test_group_by_team(self, mock_session: AsyncMock) -> None:
        team = MagicMock(spec=["id", "name"])
        team.id = _TEAM_ID
        team.name = "Alpha Team"

        row = MagicMock()
        row.team_id = _TEAM_ID
        row.total_spend_usd = Decimal("150")
        row.total_runs = 12

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(all=MagicMock(return_value=[row])),
            self._make_team_result(team),
        ])

        report = await get_cost_report(
            mock_session, org_id=_ORG_ID, group_by="team", period="month"
        )

        assert len(report) == 1
        assert report[0]["entity_id"] == str(_TEAM_ID)
        assert report[0]["entity_name"] == "Alpha Team"
        assert report[0]["total_spend_usd"] == 150.0
        assert report[0]["total_runs"] == 12

    async def test_group_by_team_unknown_team_name(
        self, mock_session: AsyncMock
    ) -> None:
        row = MagicMock()
        row.team_id = _TEAM_ID
        row.total_spend_usd = Decimal("50")
        row.total_runs = 3

        empty_scalars = MagicMock(all=MagicMock(return_value=[]))
        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(all=MagicMock(return_value=[row])),
            MagicMock(scalars=MagicMock(return_value=empty_scalars)),
        ])

        report = await get_cost_report(
            mock_session, org_id=_ORG_ID, group_by="team", period="month"
        )

        assert report[0]["entity_name"] == "Unknown"

    async def test_group_by_org(self, mock_session: AsyncMock) -> None:
        row = MagicMock()
        row.total_spend_usd = Decimal("5000")
        row.total_runs = 100

        org_result = MagicMock(scalar_one_or_none=MagicMock(return_value="My Org"))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(one=MagicMock(return_value=row)),
            org_result,
        ])

        report = await get_cost_report(
            mock_session, org_id=_ORG_ID, group_by="org", period="month"
        )

        assert len(report) == 1
        assert report[0]["entity_id"] == str(_ORG_ID)
        assert report[0]["entity_name"] == "My Org"
        assert report[0]["total_spend_usd"] == 5000.0
        assert report[0]["total_runs"] == 100

    async def test_group_by_org_empty(self, mock_session: AsyncMock) -> None:
        row = MagicMock()
        row.total_spend_usd = None
        row.total_runs = None

        org_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(one=MagicMock(return_value=row)),
            org_result,
        ])

        report = await get_cost_report(
            mock_session, org_id=_ORG_ID, group_by="org", period="month"
        )

        assert report[0]["total_spend_usd"] == 0.0
        assert report[0]["total_runs"] == 0
        assert report[0]["entity_name"] == "Unknown"

    @pytest.mark.parametrize("period", ["day", "week", "month", "year"])
    async def test_all_periods(
        self, mock_session: AsyncMock, period: str
    ) -> None:
        row = MagicMock()
        row.team_id = _TEAM_ID
        row.total_spend_usd = Decimal("10")
        row.total_runs = 1

        team = MagicMock(spec=["id", "name"])
        team.id = _TEAM_ID
        team.name = "Test Team"

        mock_session.execute = AsyncMock(side_effect=[
            MagicMock(all=MagicMock(return_value=[row])),
            self._make_team_result(team),
        ])

        report = await get_cost_report(
            mock_session, org_id=_ORG_ID, group_by="team", period=period
        )
        assert len(report) == 1
