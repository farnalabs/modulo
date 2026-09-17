"""Unit tests for OrgDailyRunCount CRUD (mocked session)."""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.daily_run_count import (
    _UNSET,
    get_daily_run_counts,
    get_org_spend_total,
    upsert_daily_run_count,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_DATE = date(2026, 1, 15)


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _existing_row(**overrides: object) -> MagicMock:
    row = MagicMock()
    row.run_count = overrides.get("run_count", 5)
    row.total_spend_usd = overrides.get("total_spend_usd", Decimal("10.00"))
    return row


def _exec_result(scalar: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar)
    return result


def _exec_scalars(items: list[object]) -> MagicMock:
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=items)
    result.scalars = MagicMock(return_value=scalars_mock)
    return result


# ── upsert_daily_run_count ──────────────────────────────────────────


class TestUpsertDailyRunCount:
    async def test_creates_new_row_when_none_exists(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(None))

        row = await upsert_daily_run_count(
            mock_session,
            org_id=_ORG_ID,
            run_date=_DATE,
            team_id=None,
            increment_count=3,
            increment_spend=Decimal("5.00"),
        )

        assert row.run_count == 3
        assert row.total_spend_usd == Decimal("5.00")
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()

    async def test_increments_existing_row(self, mock_session: AsyncMock) -> None:
        existing = _existing_row(run_count=10, total_spend_usd=Decimal("20.00"))
        mock_session.execute = AsyncMock(return_value=_exec_result(existing))

        row = await upsert_daily_run_count(
            mock_session,
            org_id=_ORG_ID,
            run_date=_DATE,
            team_id=_TEAM_ID,
            increment_count=2,
            increment_spend=Decimal("3.50"),
        )

        assert row.run_count == 12
        assert row.total_spend_usd == Decimal("23.50")
        mock_session.add.assert_not_called()
        mock_session.flush.assert_awaited_once()

    async def test_defaults_run_date_to_today(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(None))

        row = await upsert_daily_run_count(mock_session, org_id=_ORG_ID, team_id=None)

        assert row.run_count == 1
        assert row.total_spend_usd == Decimal(0)

    async def test_default_increment_is_1_with_zero_spend(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(None))

        row = await upsert_daily_run_count(mock_session, org_id=_ORG_ID, run_date=_DATE, team_id=None)

        assert row.run_count == 1
        assert row.total_spend_usd == Decimal(0)


# ── get_daily_run_counts ────────────────────────────────────────────


class TestGetDailyRunCounts:
    async def test_returns_list_of_rows(self, mock_session: AsyncMock) -> None:
        row1 = MagicMock()
        row2 = MagicMock()
        mock_session.execute = AsyncMock(return_value=_exec_scalars([row1, row2]))

        result = await get_daily_run_counts(mock_session, org_id=_ORG_ID)

        assert len(result) == 2
        assert result[0] is row1
        assert result[1] is row2

    async def test_returns_empty_when_no_rows(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        result = await get_daily_run_counts(mock_session, org_id=_ORG_ID)

        assert result == []

    async def test_team_id_unset_returns_all(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        await get_daily_run_counts(mock_session, org_id=_ORG_ID, team_id=_UNSET)

        mock_session.execute.assert_awaited_once()

    async def test_team_id_none_filters_org_level(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        await get_daily_run_counts(mock_session, org_id=_ORG_ID, team_id=None)

        mock_session.execute.assert_awaited_once()

    async def test_team_id_specific_filters_by_team(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        await get_daily_run_counts(mock_session, org_id=_ORG_ID, team_id=_TEAM_ID)

        mock_session.execute.assert_awaited_once()

    async def test_since_filter(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        await get_daily_run_counts(mock_session, org_id=_ORG_ID, since=date(2026, 1, 1))

        mock_session.execute.assert_awaited_once()

    async def test_until_filter(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        await get_daily_run_counts(mock_session, org_id=_ORG_ID, until=date(2026, 12, 31))

        mock_session.execute.assert_awaited_once()

    async def test_both_date_filters(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_scalars([]))

        await get_daily_run_counts(mock_session, org_id=_ORG_ID, since=date(2026, 1, 1), until=date(2026, 6, 30))

        mock_session.execute.assert_awaited_once()


# ── get_org_spend_total ────────────────────────────────────────────


class TestGetOrgSpendTotal:
    async def test_returns_decimal_sum(self, mock_session: AsyncMock) -> None:
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=Decimal("42.50"))
        mock_session.execute = AsyncMock(return_value=result_mock)

        total = await get_org_spend_total(mock_session, org_id=_ORG_ID)

        assert total == Decimal("42.50")

    async def test_returns_zero_when_no_rows(self, mock_session: AsyncMock) -> None:
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=result_mock)

        total = await get_org_spend_total(mock_session, org_id=_ORG_ID)

        assert total == Decimal(0)

    async def test_since_filter(self, mock_session: AsyncMock) -> None:
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=Decimal("10.00"))
        mock_session.execute = AsyncMock(return_value=result_mock)

        total = await get_org_spend_total(mock_session, org_id=_ORG_ID, since=date(2026, 1, 1))

        assert total == Decimal("10.00")
        mock_session.execute.assert_awaited_once()
