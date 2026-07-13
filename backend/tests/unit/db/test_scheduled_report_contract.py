import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.scheduled_report import compute_initial_send, create_scheduled_report
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
    assert report.recipients == []


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
