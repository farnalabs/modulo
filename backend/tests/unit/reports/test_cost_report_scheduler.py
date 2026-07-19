"""Executable scheduled cost-report lifecycle tests."""

from __future__ import annotations

import datetime
import uuid
from typing import Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.reports.cost_report import deliver_cost_report, format_cost_report, generate_cost_report
from modulo.core.reports.scheduler import DatabaseReportScheduler, _fire_scheduled_report, get_generator
from modulo.db.crud.scheduled_report import delete_scheduled_report, list_scheduled_reports
from modulo.db.models.scheduled_report import ScheduledReport


class _Begin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _Session:
    def __init__(self, results: list[MagicMock]) -> None:
        self.execute = AsyncMock(side_effect=results)
        self.delete = AsyncMock()
        self.flush = AsyncMock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()


class _Factory:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def __call__(self) -> _Session:
        return self._session


def _report(*, schedule_type: str) -> MagicMock:
    report = MagicMock(spec=ScheduledReport)
    report.id = uuid.uuid4()
    report.organisation_id = uuid.uuid4()
    report.active = True
    report.report_type = "cost"
    report.cron_expression = "0 0 * * *"
    report.config_json = {
        "period": "daily",
        "group_by": "team",
        "format": "csv",
        "schedule_type": schedule_type,
    }
    report.recipient_config = {"type": "email", "emails": ["admin@example.com"]}
    return report


def test_cost_report_generator_is_registered() -> None:
    from modulo.core.reports import _register_cost_report

    _register_cost_report()
    assert get_generator("cost") is generate_cost_report


async def test_generate_format_and_deliver_cost_report() -> None:
    rows = [{"entity_id": "team-1", "entity_name": "Platform", "total_spend_usd": 2.5, "total_runs": 4}]
    session = MagicMock(spec=AsyncSession)
    with patch(
        "modulo.core.reports.cost_report.get_cost_report", new_callable=AsyncMock, return_value=rows
    ) as get_cost:
        generated = await generate_cost_report(
            cast(AsyncSession, session),
            uuid.uuid4(),
            {"period": "weekly", "group_by": "team", "format": "csv"},
        )

    get_cost.assert_awaited_once()
    payload = format_cost_report(generated)
    assert "Platform" in payload["body_text"]
    assert "total_spend_usd" in payload["body_text"]

    with (
        patch("modulo.core.reports.cost_report.get_settings", return_value=MagicMock()),
        patch("modulo.core.reports.cost_report.send_email", return_value=True) as send,
    ):
        results = await deliver_cost_report(payload, {"type": "email", "emails": ["admin@example.com"]})

    assert results == [{"type": "email", "status": "delivered", "recipient_count": 1}]
    send.assert_called_once()


@pytest.mark.parametrize(
    ("schedule_type", "expected_active", "expected_next"),
    [
        ("one_time", False, None),
        ("recurring", True, datetime.datetime(2026, 7, 15, tzinfo=datetime.UTC)),
    ],
)
async def test_due_report_executes_and_transitions_schedule(
    schedule_type: str,
    expected_active: bool,
    expected_next: datetime.datetime | None,
) -> None:
    report = _report(schedule_type=schedule_type)
    selected = MagicMock()
    selected.scalar_one_or_none.return_value = report
    session = _Session([selected, MagicMock()])
    generator = AsyncMock(return_value={"items": [], "period": "daily", "group_by": "team", "format": "csv"})
    formatter = MagicMock(return_value={"subject": "Cost", "body_html": "<p />", "body_text": "cost"})
    deliverer = AsyncMock(return_value=[{"type": "email", "status": "delivered", "recipient_count": 1}])

    with (
        patch("modulo.core.reports.scheduler._get_engine"),
        patch("modulo.core.reports.scheduler.async_sessionmaker", return_value=_Factory(session)),
        patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.reports.scheduler.get_generator", return_value=generator),
        patch("modulo.core.reports.scheduler.get_formatter", return_value=formatter),
        patch("modulo.core.reports.scheduler.get_deliverer", return_value=deliverer),
        patch("modulo.core.reports.scheduler.compute_next_send", return_value=expected_next),
    ):
        result = await _fire_scheduled_report(report_id=report.id, org_id=report.organisation_id)

    assert result["status"] == "sent"
    assert result["next_send_at"] == (expected_next.isoformat() if expected_next else None)
    generator.assert_awaited_once()
    deliverer.assert_awaited_once()
    update_statement = session.execute.await_args_list[1].args[0]
    update_values = {column.key: value.value for column, value in update_statement._values.items()}
    assert update_values["active"] is expected_active
    assert update_values["next_send_at"] == expected_next


async def test_failed_delivery_does_not_deactivate_one_time_report() -> None:
    report = _report(schedule_type="one_time")
    selected = MagicMock()
    selected.scalar_one_or_none.return_value = report
    session = _Session([selected])
    generator = AsyncMock(return_value={"items": [], "period": "daily", "group_by": "team", "format": "csv"})
    deliverer = AsyncMock(side_effect=RuntimeError("SMTP unavailable"))

    with (
        patch("modulo.core.reports.scheduler._get_engine"),
        patch("modulo.core.reports.scheduler.async_sessionmaker", return_value=_Factory(session)),
        patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.reports.scheduler.get_generator", return_value=generator),
        patch("modulo.core.reports.scheduler.get_formatter", return_value=MagicMock()),
        patch("modulo.core.reports.scheduler.get_deliverer", return_value=deliverer),
    ):
        result = await _fire_scheduled_report(report_id=report.id, org_id=report.organisation_id)

    assert result == {"status": "failed", "reason": "generation_or_delivery_failed"}
    assert session.execute.await_count == 1


async def test_due_selection_requires_active_non_null_due_time() -> None:
    result = MagicMock()
    result.all.return_value = []
    session = _Session([result])
    scheduler = object.__new__(DatabaseReportScheduler)

    with (
        patch("modulo.core.reports.scheduler._get_engine"),
        patch("modulo.core.reports.scheduler.async_sessionmaker", return_value=_Factory(session)),
    ):
        assert await scheduler._fetch_due_reports() == []

    statement = session.execute.await_args_list[0].args[0]
    sql = str(statement)
    assert "scheduled_reports.active = true" in sql
    assert "scheduled_reports.next_send_at <=" in sql


async def test_cost_crud_filters_and_cannot_delete_quality_report() -> None:
    listed = MagicMock()
    listed.scalars.return_value.all.return_value = []
    session = _Session([listed])
    org_id = uuid.uuid4()

    assert await list_scheduled_reports(cast(AsyncSession, session), organisation_id=org_id) == []
    list_sql = str(session.execute.await_args_list[0].args[0])
    assert "scheduled_reports.report_type =" in list_sql

    missing = MagicMock()
    missing.scalar_one_or_none.return_value = None
    session = _Session([missing])
    deleted = await delete_scheduled_report(
        cast(AsyncSession, session),
        report_id=uuid.uuid4(),
        organisation_id=org_id,
    )
    assert deleted is False
    delete_lookup_sql = str(session.execute.await_args_list[0].args[0])
    assert "scheduled_reports.report_type =" in delete_lookup_sql
    session.delete.assert_not_awaited()
