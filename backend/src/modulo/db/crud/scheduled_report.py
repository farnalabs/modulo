"""CRUD for ScheduledReport records.

All functions require RLS org context to be set by the caller.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.scheduled_report import ScheduledReport

_COST_REPORT_CRON = {
    "daily": "0 0 * * *",
    "weekly": "0 0 * * 1",
    "monthly": "0 0 1 * *",
}


def compute_initial_send(period: str, *, after: datetime | None = None) -> datetime:
    """Return the first UTC occurrence for a cost report period."""
    try:
        expression = _COST_REPORT_CRON[period]
    except KeyError:
        raise ValueError(f"Unsupported cost report period: {period}") from None
    base = after or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    next_send = croniter(expression, base.astimezone(UTC)).get_next(datetime)
    if not isinstance(next_send, datetime):
        raise TypeError(f"croniter returned unexpected type: {type(next_send)}")
    return next_send.astimezone(UTC)


async def create_scheduled_report(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    period: str,
    group_by: str,
    format: str,
    recipients: list[str],
    schedule_type: str,
    account_id: uuid.UUID,
    next_run_at: datetime | None = None,
) -> ScheduledReport:
    if group_by not in {"team", "org"}:
        raise ValueError(f"Unsupported cost report grouping: {group_by}")
    if format not in {"csv", "json"}:
        raise ValueError(f"Unsupported cost report format: {format}")
    if schedule_type not in {"one_time", "recurring"}:
        raise ValueError(f"Unsupported cost report schedule type: {schedule_type}")

    cron_expression = _COST_REPORT_CRON.get(period)
    if cron_expression is None:
        raise ValueError(f"Unsupported cost report period: {period}")
    if next_run_at is not None and next_run_at.tzinfo is None:
        raise ValueError("Scheduled cost report next_run_at must be timezone-aware")
    first_send = next_run_at.astimezone(UTC) if next_run_at is not None else compute_initial_send(period)
    report = ScheduledReport(
        organisation_id=organisation_id,
        name=f"Cost report: {period} by {group_by}",
        report_type="cost",
        cron_expression=cron_expression,
        config_json={
            "period": period,
            "group_by": group_by,
            "format": format,
            "schedule_type": schedule_type,
        },
        recipient_config={"type": "email", "emails": recipients},
        created_by=account_id,
        next_send_at=first_send,
        active=True,
    )
    session.add(report)
    await session.flush()
    return report


async def list_scheduled_reports(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
) -> Sequence[ScheduledReport]:
    q = (
        select(ScheduledReport)
        .where(
            ScheduledReport.organisation_id == organisation_id,
            ScheduledReport.report_type == "cost",
        )
        .order_by(ScheduledReport.created_at.desc())
    )
    result = await session.execute(q)
    return list(result.scalars().all())


async def get_scheduled_report(
    session: AsyncSession,
    *,
    report_id: uuid.UUID,
    organisation_id: uuid.UUID,
) -> ScheduledReport | None:
    q = select(ScheduledReport).where(
        ScheduledReport.id == report_id,
        ScheduledReport.organisation_id == organisation_id,
        ScheduledReport.report_type == "cost",
    )
    result = await session.execute(q)
    return result.scalar_one_or_none()


async def delete_scheduled_report(
    session: AsyncSession,
    *,
    report_id: uuid.UUID,
    organisation_id: uuid.UUID,
) -> bool:
    report = await get_scheduled_report(
        session,
        report_id=report_id,
        organisation_id=organisation_id,
    )
    if report is None:
        return False
    await session.delete(report)
    await session.flush()
    return True
