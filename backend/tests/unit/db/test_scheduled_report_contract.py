import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.scheduled_report import (
    compute_initial_send,
    create_scheduled_report,
    delete_scheduled_report,
)
from modulo.db.models.scheduled_report import ScheduledReport


def test_cost_report_compatibility_properties_read_canonical_json() -> None:
    report = ScheduledReport(
        organisation_id=uuid.uuid4(),
        name="Weekly cost report",
        report_type="cost",
        cron_expression="0 0 * * 1",
        config_json={"period": "weekly", "group_by": "team", "format": "csv", "schedule_type": "recurring"},
        recipient_config={"type": "email", "emails": ["admin@example.com", 42]},
    )

    assert report.period == "weekly"
    assert report.group_by == "team"
    assert report.format == "csv"
    assert report.schedule_type == "recurring"
    assert report.recipients == ["admin@example.com"]


def test_non_cost_report_does_not_invent_cost_compatibility_values() -> None:
    report = ScheduledReport(
        organisation_id=uuid.uuid4(),
        name="Quality report",
        report_type="quality",
        cron_expression="0 9 * * 1",
        config_json={},
        recipient_config={"webhook_urls": ["https://example.com/hook"]},
    )

    assert report.period is None
    assert report.group_by is None
    assert report.format is None
    assert report.schedule_type is None
    assert not report.recipients


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("daily", datetime(2026, 7, 14, tzinfo=UTC)),
        ("weekly", datetime(2026, 7, 20, tzinfo=UTC)),
        ("monthly", datetime(2026, 8, 1, tzinfo=UTC)),
    ],
)
def test_compute_initial_send_uses_next_utc_boundary(period: str, expected: datetime) -> None:
    after = datetime(2026, 7, 13, 12, 30, tzinfo=UTC)
    assert compute_initial_send(period, after=after) == expected


@pytest.mark.asyncio
async def test_create_cost_report_maps_to_scheduler_schema() -> None:
    session = MagicMock(spec=AsyncSession)
    session.flush = AsyncMock()
    account_id = uuid.uuid4()

    report = await create_scheduled_report(
        cast(AsyncSession, session),
        organisation_id=uuid.uuid4(),
        period="monthly",
        group_by="team",
        format="json",
        recipients=["owner@example.com"],
        schedule_type="recurring",
        account_id=account_id,
        next_run_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert report.report_type == "cost"
    assert report.cron_expression == "0 0 1 * *"
    assert report.config_json == {
        "period": "monthly",
        "group_by": "team",
        "format": "json",
        "schedule_type": "recurring",
    }
    assert report.recipient_config == {"type": "email", "emails": ["owner@example.com"]}
    assert report.next_send_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert report.created_by == account_id
    session.add.assert_called_once_with(report)
    session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# create_scheduled_report — every rejection branch
# ---------------------------------------------------------------------------

_VALID_CREATE_KWARGS = {
    "period": "monthly",
    "group_by": "team",
    "format": "csv",
    "recipients": ["owner@example.com"],
    "schedule_type": "recurring",
    "account_id": None,  # filled per-test with a fresh UUID
}

# Parametrized branch -> (field override, exact ValueError message). The tz
# check runs *after* the period check in production, so that case keeps a
# valid period and passes a naive datetime instead.
_REJECTION_CASES = [
    ({"group_by": "user"}, "Unsupported cost report grouping: user"),
    ({"format": "pdf"}, "Unsupported cost report format: pdf"),
    ({"schedule_type": "adhoc"}, "Unsupported cost report schedule type: adhoc"),
    ({"period": "hourly"}, "Unsupported cost report period: hourly"),
    ({"next_run_at": datetime(2026, 8, 1)}, "Scheduled cost report next_run_at must be timezone-aware"),
]


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    _REJECTION_CASES,
)
@pytest.mark.asyncio
async def test_create_cost_report_rejects_invalid_input_without_persisting(
    overrides: dict[str, object],
    expected_message: str,
) -> None:
    session = MagicMock(spec=AsyncSession)
    session.add = MagicMock()
    session.flush = AsyncMock()

    with pytest.raises(ValueError, match=expected_message):
        await create_scheduled_report(
            cast(AsyncSession, session),
            organisation_id=uuid.uuid4(),
            **{**_VALID_CREATE_KWARGS, "account_id": uuid.uuid4(), **overrides},
        )

    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_cost_report_without_next_run_at_computes_first_utc_boundary() -> None:
    """With no explicit ``next_run_at``, the first send must be the next
    period boundary asserted property-by-property (independent of croniter):
    midnight UTC, on the monthly day-of-month, strictly in the future."""
    session = MagicMock(spec=AsyncSession)
    session.flush = AsyncMock()
    before = datetime.now(UTC).astimezone(UTC)

    report = await create_scheduled_report(
        cast(AsyncSession, session),
        organisation_id=uuid.uuid4(),
        period="monthly",
        group_by="org",
        format="json",
        recipients=["owner@example.com"],
        schedule_type="one_time",
        account_id=uuid.uuid4(),
    )

    first_send = report.next_send_at
    assert first_send.tzinfo is UTC
    assert first_send > before
    assert (first_send.day, first_send.hour, first_send.minute, first_send.second) == (1, 0, 0, 0)
    session.add.assert_called_once_with(report)
    session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# compute_initial_send — direct gate branches
# ---------------------------------------------------------------------------


def test_compute_initial_send_rejects_unsupported_period() -> None:
    with pytest.raises(ValueError, match="Unsupported cost report period: fortnightly"):
        compute_initial_send("fortnightly")


def test_compute_initial_send_treats_naive_after_as_utc() -> None:
    """A naive ``after`` is accepted and read as UTC, not as local wall time.
    A half-past-midnight input pins the distinction: read as UTC, 2026-07-13
    00:30 is still inside the day, so the daily boundary is midnight on the
    14th — while a local-zone reinterpretation in any zone ahead of UTC falls
    back into July 12 and answers midnight on the 13th. (``next_run_at``,
    by contrast, is rejected outright when naive — see the create tests.)"""
    after = datetime(2026, 7, 13, 0, 30)
    assert compute_initial_send("daily", after=after) == datetime(2026, 7, 14, tzinfo=UTC)


def test_compute_initial_send_rejects_unexpected_croniter_type() -> None:
    """If croniter's result shape ever changes, the contract fails loudly with
    a TypeError naming the type instead of leaking a string into a datetime
    column."""
    with patch("modulo.db.crud.scheduled_report.croniter") as croniter_cls:
        croniter_cls.return_value.get_next.return_value = "2026-07-14"
        with pytest.raises(TypeError, match=r"croniter returned unexpected type: <class 'str'>"):
            compute_initial_send("daily", after=datetime(2026, 7, 13, tzinfo=UTC))


# ---------------------------------------------------------------------------
# delete_scheduled_report — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_cost_report_removes_row_and_returns_true() -> None:
    """The success path: the scavenged report is selected, deleted and flushed,
    and the caller sees ``True``. The result fake is a lightweight object with
    only the reader production calls (``scalar_one_or_none``), so a renamed
    reader surfaces as a loud AttributeError instead of a MagicMock default.
    The missing-row path (returns False) is covered in
    tests/unit/reports/test_cost_report_scheduler.py."""
    org_id = uuid.uuid4()
    report = ScheduledReport(
        organisation_id=org_id,
        name="Cost report: monthly by team",
        report_type="cost",
        cron_expression="0 0 1 * *",
        config_json={"period": "monthly", "group_by": "team", "format": "csv", "schedule_type": "recurring"},
        recipient_config={"type": "email", "emails": ["owner@example.com"]},
    )
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: report))
    session.delete = AsyncMock()
    session.flush = AsyncMock()

    deleted = await delete_scheduled_report(
        cast(AsyncSession, session),
        report_id=uuid.uuid4(),
        organisation_id=org_id,
    )

    assert deleted is True
    session.delete.assert_awaited_once_with(report)
    session.flush.assert_awaited_once()
