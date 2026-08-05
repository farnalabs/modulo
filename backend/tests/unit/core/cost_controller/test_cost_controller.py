"""Unit tests for modulo.core.cost_controller.

All DB interaction is mocked; we verify logic, limit enforcement,
and correct SQL filtering.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from modulo.core.cost_controller import (
    check_and_record_spend,
    get_cost_report,
    get_or_create_daily_count,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TODAY = date(2026, 6, 24)
_FROZEN = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def mock_session() -> AsyncMock:
    s = AsyncMock()
    s.add = MagicMock()
    return s


@pytest.fixture(autouse=True)
def _freeze_datetime() -> Iterator[None]:
    with patch("modulo.core.cost_controller.datetime") as mock_dt:
        mock_dt.now.return_value = _FROZEN
        mock_dt.UTC = UTC
        mock_dt.date = date
        mock_dt.timedelta = timedelta
        mock_dt.datetime = datetime
        yield


def _make_daily_count_row(**kw: object) -> MagicMock:
    row = MagicMock()
    row.id = kw.get("id", uuid.uuid4())
    row.organisation_id = kw.get("organisation_id", _ORG_ID)
    row.team_id = kw.get("team_id")
    row.run_date = kw.get("run_date", _TODAY)
    row.run_count = kw.get("run_count", 0)
    row.total_spend_usd = kw.get("total_spend_usd", Decimal(0))
    row.clamped = kw.get("clamped", False)
    row.refused_spend_usd = kw.get("refused_spend_usd", Decimal(0))
    return row


# ---------------------------------------------------------------------------
# get_or_create_daily_count
# ---------------------------------------------------------------------------


class TestGetOrCreateDailyCount:
    async def test_returns_existing_row(self, mock_session: AsyncMock) -> None:
        existing = _make_daily_count_row(run_count=5, total_spend_usd=Decimal("12.50"))
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing)))

        result = await get_or_create_daily_count(mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None)

        assert result is existing
        assert result.run_count == 5
        assert result.total_spend_usd == Decimal("12.50")

    async def test_creates_new_row_when_missing(self, mock_session: AsyncMock) -> None:
        first = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        lock = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[first, lock])

        result = await get_or_create_daily_count(mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None)

        assert result.run_count == 0
        assert result.total_spend_usd == Decimal(0)
        mock_session.add.assert_called_once()
        added = mock_session.add.call_args.args[0]
        assert added.organisation_id == _ORG_ID
        assert added.run_date == _TODAY
        assert added.team_id is None
        assert added.run_count == 0
        assert added.total_spend_usd == Decimal(0)
        mock_session.flush.assert_awaited_once()

    async def test_creates_new_team_row(self, mock_session: AsyncMock) -> None:
        first = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        lock = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[first, lock])

        result = await get_or_create_daily_count(mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=_TEAM_ID)

        assert result.team_id == _TEAM_ID
        mock_session.add.assert_called_once()
        added = mock_session.add.call_args.args[0]
        assert added.organisation_id == _ORG_ID
        assert added.run_date == _TODAY
        assert added.team_id == _TEAM_ID
        assert added.run_count == 0
        assert added.total_spend_usd == Decimal(0)

    async def test_uses_select_for_update(self, mock_session: AsyncMock) -> None:
        existing = _make_daily_count_row()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing)))

        await get_or_create_daily_count(mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None)

        call = mock_session.execute.call_args[0][0]
        assert "FOR UPDATE" in str(call).upper()

    async def test_integrity_error_returns_row_written_by_race(self, mock_session: AsyncMock) -> None:
        """Concurrent insert: flush raises, savepoint rolls back, re-query returns the winning row."""
        winner = _make_daily_count_row(run_count=1, total_spend_usd=Decimal(7))
        savepoint = AsyncMock()
        savepoint.rollback = AsyncMock()
        mock_session.begin_nested = AsyncMock(return_value=savepoint)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")))

        first = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        requery = MagicMock(scalar_one_or_none=MagicMock(return_value=winner))
        mock_session.execute = AsyncMock(side_effect=[first, requery])

        result = await get_or_create_daily_count(mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None)

        assert result is winner
        savepoint.rollback.assert_awaited_once()
        mock_session.add.assert_called_once()
        assert mock_session.execute.await_count == 2

    async def test_integrity_error_re_raises_when_row_still_missing(self, mock_session: AsyncMock) -> None:
        """After rollback the row is still absent: the error is propagated to the caller."""
        savepoint = AsyncMock()
        savepoint.rollback = AsyncMock()
        mock_session.begin_nested = AsyncMock(return_value=savepoint)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")))

        first = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        requery = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        mock_session.execute = AsyncMock(side_effect=[first, requery])

        with pytest.raises(IntegrityError):
            await get_or_create_daily_count(mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None)

        savepoint.rollback.assert_awaited_once()
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        assert mock_session.execute.await_count == 2

    async def test_cancelled_error_is_not_swallowed(self, mock_session: AsyncMock) -> None:
        savepoint = AsyncMock()
        mock_session.begin_nested = AsyncMock(return_value=savepoint)
        mock_session.flush = AsyncMock(side_effect=asyncio.CancelledError())

        first = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        mock_session.execute = AsyncMock(return_value=first)

        with pytest.raises(asyncio.CancelledError):
            await get_or_create_daily_count(mock_session, org_id=_ORG_ID, run_date=_TODAY, team_id=None)

        savepoint.rollback.assert_not_awaited()
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        assert mock_session.execute.await_count == 1


# ---------------------------------------------------------------------------
# check_and_record_spend
# ---------------------------------------------------------------------------


class TestCheckAndRecordSpend:
    """The refusal-window contract (spec §4.6).

    The limit check is keyed to the CREATED-AT day (``created_at_day_start``),
    the current run is EXCLUDED from each SUM (``id != :current_run_id``) and
    ``cost_usd`` added UNCONDITIONALLY (counted EXACTLY ONCE per predicate).
    The SUMs SHORT-CIRCUIT when the limit is NULL (a no-limit org runs NO SUM).
    BOTH limits are checked BEFORE either spend row is written; a refusal
    writes the refused amount to the day rows' ``refused_spend_usd``.
    """

    def _mock_execute(self, *results: MagicMock) -> AsyncMock:
        return AsyncMock(side_effect=list(results))

    def _scalar_none(self) -> MagicMock:
        return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    def _scalar_one(self, value: object) -> MagicMock:
        return MagicMock(scalar_one=MagicMock(return_value=value), scalar_one_or_none=MagicMock(return_value=value))

    async def test_org_no_limit_runs_no_sum(self, mock_session: AsyncMock) -> None:
        """A no-limit org (the default) runs NO created-at SUM and never refuses."""
        org_count = _make_daily_count_row(run_count=2, total_spend_usd=Decimal(10))
        mock_session.execute = self._mock_execute(self._scalar_none())
        with patch(
            "modulo.core.cost_controller.get_or_create_daily_count",
            new=AsyncMock(return_value=org_count),
        ):
            approved, reason = await check_and_record_spend(
                mock_session,
                org_id=_ORG_ID,
                cost_usd=Decimal("5.50"),
                team_id=None,
                run_id=uuid.uuid4(),
                run_date=_TODAY,
            )

        assert approved is True
        assert reason is None
        assert org_count.total_spend_usd == Decimal("15.50")
        assert org_count.run_count == 3
        assert mock_session.execute.await_count == 1  # org-limit fetch ONLY — no SUM

    async def test_approves_below_limit(self, mock_session: AsyncMock) -> None:
        """Day one-cost below the limit accepts."""
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal(0))
        mock_session.execute = self._mock_execute(
            self._scalar_one(Decimal(100)),  # org limit
            self._scalar_one(Decimal(50)),  # created-at SUM (excludes current)
        )
        with patch("modulo.core.cost_controller.get_or_create_daily_count", new=AsyncMock(return_value=org_count)):
            approved, reason = await check_and_record_spend(
                mock_session, org_id=_ORG_ID, cost_usd=Decimal(5), team_id=None, run_id=uuid.uuid4(), run_date=_TODAY
            )

        assert approved is True
        assert reason is None
        assert org_count.total_spend_usd == Decimal(5)
        assert org_count.run_count == 1
        mock_session.flush.assert_awaited_once()

    async def test_accepts_at_half_limit(self, mock_session: AsyncMock) -> None:
        """A terminal at HALF the limit accepts — the current run is counted ONCE."""
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal(0))
        mock_session.execute = self._mock_execute(
            self._scalar_one(Decimal(100)),
            self._scalar_one(Decimal(40)),  # other runs at 40; +10 current = 50 (half)
        )
        with patch("modulo.core.cost_controller.get_or_create_daily_count", new=AsyncMock(return_value=org_count)):
            approved, _ = await check_and_record_spend(
                mock_session, org_id=_ORG_ID, cost_usd=Decimal(10), team_id=None, run_id=uuid.uuid4(), run_date=_TODAY
            )

        assert approved is True
        assert org_count.total_spend_usd == Decimal(10)

    async def test_refuses_exactly_at_limit(self, mock_session: AsyncMock) -> None:
        """The day's other runs at EXACTLY the limit -> any positive cost refuses
        AT the configured limit (never at half)."""
        org_refused = _make_daily_count_row(run_count=0, total_spend_usd=Decimal(0))
        mock_session.execute = self._mock_execute(
            self._scalar_one(Decimal(100)),
            self._scalar_one(Decimal(100)),  # other runs AT the limit
        )
        with patch("modulo.core.cost_controller.get_or_create_daily_count", new=AsyncMock(return_value=org_refused)):
            approved, reason = await check_and_record_spend(
                mock_session, org_id=_ORG_ID, cost_usd=Decimal(5), team_id=None, run_id=uuid.uuid4(), run_date=_TODAY
            )

        assert approved is False
        assert reason == "daily_limit_exceeded"
        # No spend row — a refused-only row carrying the refused amount.
        assert org_refused.run_count == 0
        assert org_refused.total_spend_usd == Decimal(0)
        assert org_refused.refused_spend_usd == Decimal(5)

    async def test_refuses_over_limit(self, mock_session: AsyncMock) -> None:
        org_refused = _make_daily_count_row(run_count=0, total_spend_usd=Decimal(0))
        mock_session.execute = self._mock_execute(
            self._scalar_one(Decimal(100)),
            self._scalar_one(Decimal(95)),
        )
        with patch("modulo.core.cost_controller.get_or_create_daily_count", new=AsyncMock(return_value=org_refused)):
            approved, reason = await check_and_record_spend(
                mock_session, org_id=_ORG_ID, cost_usd=Decimal(10), team_id=None, run_id=uuid.uuid4(), run_date=_TODAY
            )

        assert approved is False
        assert reason == "daily_limit_exceeded"
        assert org_refused.refused_spend_usd == Decimal(10)
        assert org_refused.run_count == 0
        mock_session.flush.assert_awaited_once()

    async def test_approves_with_team_under_both_limits(self, mock_session: AsyncMock) -> None:
        org_count = _make_daily_count_row(run_count=1, total_spend_usd=Decimal(30))
        team_count = _make_daily_count_row(team_id=_TEAM_ID, run_count=0, total_spend_usd=Decimal(10))
        mock_session.execute = self._mock_execute(
            self._scalar_one(Decimal(200)),  # org limit
            self._scalar_one(Decimal(100)),  # org SUM
            self._scalar_one(Decimal(50)),  # team limit
            self._scalar_one(Decimal(30)),  # team SUM
        )
        with patch(
            "modulo.core.cost_controller.get_or_create_daily_count",
            new=AsyncMock(side_effect=[org_count, team_count]),
        ):
            approved, reason = await check_and_record_spend(
                mock_session,
                org_id=_ORG_ID,
                cost_usd=Decimal(5),
                team_id=_TEAM_ID,
                run_id=uuid.uuid4(),
                run_date=_TODAY,
            )

        assert approved is True
        assert reason is None
        # Org row written first, then the team row (org-then-team mutation order).
        assert org_count.total_spend_usd == Decimal(35)
        assert org_count.run_count == 2
        assert team_count.total_spend_usd == Decimal(15)
        assert team_count.run_count == 1

    async def test_org_passes_team_fails_writes_neither_spend_row(self, mock_session: AsyncMock) -> None:
        """An org-passing / team-failing run refuses and writes NEITHER spend row;
        the refused amount is written to BOTH rows' refused_spend_usd."""
        org_refused = _make_daily_count_row(run_count=1, total_spend_usd=Decimal(5))
        team_refused = _make_daily_count_row(team_id=_TEAM_ID, run_count=2, total_spend_usd=Decimal(45))
        mock_session.execute = self._mock_execute(
            self._scalar_one(Decimal(200)),
            self._scalar_one(Decimal(50)),
            self._scalar_one(Decimal(50)),
            self._scalar_one(Decimal(45)),
        )
        with patch(
            "modulo.core.cost_controller.get_or_create_daily_count",
            new=AsyncMock(side_effect=[org_refused, team_refused]),
        ):
            approved, reason = await check_and_record_spend(
                mock_session,
                org_id=_ORG_ID,
                cost_usd=Decimal(10),
                team_id=_TEAM_ID,
                run_id=uuid.uuid4(),
                run_date=_TODAY,
            )

        assert approved is False
        assert reason == "daily_limit_exceeded"
        assert org_refused.run_count == 1  # unchanged
        assert org_refused.total_spend_usd == Decimal(5)  # unchanged
        assert org_refused.refused_spend_usd == Decimal(10)
        assert team_refused.run_count == 2  # unchanged
        assert team_refused.total_spend_usd == Decimal(45)  # unchanged
        assert team_refused.refused_spend_usd == Decimal(10)

    async def test_approves_when_both_limits_none(self, mock_session: AsyncMock) -> None:
        """No-limit org AND no-limit team accept every terminal run."""
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal(0))
        team_count = _make_daily_count_row(team_id=_TEAM_ID, run_count=0, total_spend_usd=Decimal(0))
        mock_session.execute = self._mock_execute(
            self._scalar_none(),  # org limit NULL
            self._scalar_none(),  # team limit NULL
        )
        with patch(
            "modulo.core.cost_controller.get_or_create_daily_count",
            new=AsyncMock(side_effect=[org_count, team_count]),
        ):
            approved, reason = await check_and_record_spend(
                mock_session,
                org_id=_ORG_ID,
                cost_usd=Decimal(99999),
                team_id=_TEAM_ID,
                run_id=uuid.uuid4(),
                run_date=_TODAY,
            )

        assert approved is True
        assert reason is None
        assert mock_session.execute.await_count == 2  # limit fetches only — no SUMs

    async def test_null_owner_writes_org_row_only(self, mock_session: AsyncMock) -> None:
        """A NULL-owner run (team_id None) writes ONLY the org row — no team row."""
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal(0))
        mock_session.execute = self._mock_execute(
            self._scalar_none(),
        )
        with patch("modulo.core.cost_controller.get_or_create_daily_count", new=AsyncMock(return_value=org_count)):
            approved, reason = await check_and_record_spend(
                mock_session, org_id=_ORG_ID, cost_usd=Decimal(5), team_id=None, run_id=uuid.uuid4(), run_date=_TODAY
            )

        assert approved is True
        assert reason is None
        assert org_count.run_count == 1
        assert org_count.total_spend_usd == Decimal(5)

    @pytest.mark.parametrize(
        ("cost_usd", "reason_keyword"),
        [
            (Decimal(-5), "non_negative"),
            (None, "none"),
            (Decimal("NaN"), "finite"),
            (Decimal("Infinity"), "finite"),
        ],
    )
    async def test_rejects_invalid_cost(
        self, mock_session: AsyncMock, cost_usd: Decimal | None, reason_keyword: str
    ) -> None:
        approved, reason = await check_and_record_spend(
            mock_session,
            org_id=_ORG_ID,
            cost_usd=cost_usd,
            team_id=None,
            run_id=uuid.uuid4(),
            run_date=_TODAY,
        )

        assert approved is False
        assert reason_keyword in (reason or "").lower()
        mock_session.execute.assert_not_called()

    async def test_approves_zero_cost(self, mock_session: AsyncMock) -> None:
        org_count = _make_daily_count_row(run_count=1, total_spend_usd=Decimal(10))
        mock_session.execute = self._mock_execute(self._scalar_none())
        with patch("modulo.core.cost_controller.get_or_create_daily_count", new=AsyncMock(return_value=org_count)):
            approved, reason = await check_and_record_spend(
                mock_session, org_id=_ORG_ID, cost_usd=Decimal(0), team_id=None, run_id=uuid.uuid4(), run_date=_TODAY
            )

        assert approved is True
        assert reason is None
        assert org_count.run_count == 2

    async def test_daily_ledger_clamp_sets_clamped(self, mock_session: AsyncMock) -> None:
        """The started-at-day row over the column ceiling is clamped + flagged."""
        org_count = _make_daily_count_row(run_count=0, total_spend_usd=Decimal("99999999.999998"))
        mock_session.execute = self._mock_execute(self._scalar_none())
        with patch("modulo.core.cost_controller.get_or_create_daily_count", new=AsyncMock(return_value=org_count)):
            approved, reason = await check_and_record_spend(
                mock_session, org_id=_ORG_ID, cost_usd=Decimal(10), team_id=None, run_id=uuid.uuid4(), run_date=_TODAY
            )

        assert approved is True
        assert reason is None
        assert org_count.total_spend_usd == Decimal("99999999.999999")
        assert org_count.clamped is True


# ---------------------------------------------------------------------------
# get_cost_report
# ---------------------------------------------------------------------------


class TestGetCostReport:
    def _make_team_result(self, team: MagicMock) -> MagicMock:
        scalars_mock = MagicMock(all=MagicMock(return_value=[team]))
        return MagicMock(scalars=MagicMock(return_value=scalars_mock))

    async def test_group_by_team(self, mock_session: AsyncMock) -> None:
        team = MagicMock(spec=["id", "name"])
        team.id = _TEAM_ID
        team.name = "Alpha Team"

        row = MagicMock()
        row.team_id = _TEAM_ID
        row.total_spend_usd = Decimal(150)
        row.total_runs = 12

        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(all=MagicMock(return_value=[row])),
                self._make_team_result(team),
            ]
        )

        report = await get_cost_report(mock_session, org_id=_ORG_ID, group_by="team", period="month")

        assert len(report) == 1
        assert report[0]["entity_id"] == str(_TEAM_ID)
        assert report[0]["entity_name"] == "Alpha Team"
        assert report[0]["total_spend_usd"] == 150.0
        assert report[0]["total_runs"] == 12

    async def test_group_by_team_unknown_team_name(self, mock_session: AsyncMock) -> None:
        row = MagicMock()
        row.team_id = _TEAM_ID
        row.total_spend_usd = Decimal(50)
        row.total_runs = 3

        empty_scalars = MagicMock(all=MagicMock(return_value=[]))
        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(all=MagicMock(return_value=[row])),
                MagicMock(scalars=MagicMock(return_value=empty_scalars)),
            ]
        )

        report = await get_cost_report(mock_session, org_id=_ORG_ID, group_by="team", period="month")

        assert report[0]["entity_name"] == "Unknown"

    async def test_group_by_org(self, mock_session: AsyncMock) -> None:
        row = MagicMock()
        row.total_spend_usd = Decimal(5000)
        row.total_runs = 100

        org_result = MagicMock(scalar_one_or_none=MagicMock(return_value="My Org"))

        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=row)),
                org_result,
            ]
        )

        report = await get_cost_report(mock_session, org_id=_ORG_ID, group_by="org", period="month")

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

        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=row)),
                org_result,
            ]
        )

        report = await get_cost_report(mock_session, org_id=_ORG_ID, group_by="org", period="month")

        assert report[0]["total_spend_usd"] == 0.0
        assert report[0]["total_runs"] == 0
        assert report[0]["entity_name"] == "Unknown"

    @pytest.mark.parametrize("period", ["day", "week", "month", "year"])
    async def test_all_periods(self, mock_session: AsyncMock, period: str) -> None:
        row = MagicMock()
        row.team_id = _TEAM_ID
        row.total_spend_usd = Decimal(10)
        row.total_runs = 1

        team = MagicMock(spec=["id", "name"])
        team.id = _TEAM_ID
        team.name = "Test Team"

        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(all=MagicMock(return_value=[row])),
                self._make_team_result(team),
            ]
        )

        report = await get_cost_report(mock_session, org_id=_ORG_ID, group_by="team", period=period)
        assert len(report) == 1

    async def test_group_by_team_empty(self, mock_session: AsyncMock) -> None:
        empty_scalars = MagicMock(all=MagicMock(return_value=[]))
        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(all=MagicMock(return_value=[])),
                MagicMock(scalars=MagicMock(return_value=empty_scalars)),
            ]
        )

        report = await get_cost_report(mock_session, org_id=_ORG_ID, group_by="team", period="month")
        assert report == []

    async def test_group_by_team_skips_rows_without_team_id(self, mock_session: AsyncMock) -> None:
        """Org-level rows (team_id is None) are filtered out of the team report.

        The SQL query already filters ``team_id IS NOT NULL`` (asserted below), so
        this exercises the Python-side defensive guard for rows that slip through.
        """
        org_row = MagicMock()
        org_row.team_id = None
        org_row.total_spend_usd = Decimal(900)
        org_row.total_runs = 9

        team_row = MagicMock()
        team_row.team_id = _TEAM_ID
        team_row.total_spend_usd = Decimal(50)
        team_row.total_runs = 3

        team = MagicMock(spec=["id", "name"])
        team.id = _TEAM_ID
        team.name = "Alpha Team"

        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(all=MagicMock(return_value=[org_row, team_row])),
                self._make_team_result(team),
            ]
        )

        report = await get_cost_report(mock_session, org_id=_ORG_ID, group_by="team", period="month")

        assert len(report) == 1
        assert report[0]["entity_id"] == str(_TEAM_ID)
        assert report[0]["entity_name"] == "Alpha Team"
        assert report[0]["total_spend_usd"] == 50.0
        assert report[0]["total_runs"] == 3

        q = mock_session.execute.call_args_list[0].args[0]
        assert "team_id IS NOT NULL" in str(q.compile())

    @pytest.mark.parametrize(
        ("period", "expected_since"),
        [
            ("day", date(2026, 6, 24)),
            ("week", date(2026, 6, 22)),
            ("month", date(2026, 6, 1)),
            ("year", date(2026, 1, 1)),
        ],
    )
    async def test_since_date_per_period(self, mock_session: AsyncMock, period: str, expected_since: date) -> None:
        empty_scalars = MagicMock(all=MagicMock(return_value=[]))
        mock_session.execute = AsyncMock(
            side_effect=[
                MagicMock(all=MagicMock(return_value=[])),
                MagicMock(scalars=MagicMock(return_value=empty_scalars)),
            ]
        )

        await get_cost_report(mock_session, org_id=_ORG_ID, group_by="team", period=period)

        q = mock_session.execute.call_args_list[0].args[0]
        params = q.compile().params
        assert expected_since in params.values()

    async def test_invalid_period_raises(self, mock_session: AsyncMock) -> None:
        with pytest.raises(ValueError, match="Unknown period"):
            await get_cost_report(mock_session, org_id=_ORG_ID, group_by="team", period="century")

    async def test_invalid_group_by_raises(self, mock_session: AsyncMock) -> None:
        with pytest.raises(ValueError, match="Unknown group_by"):
            await get_cost_report(mock_session, org_id=_ORG_ID, group_by="department", period="month")
