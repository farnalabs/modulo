"""CRUD for ScheduledReport records.

All functions require RLS org context to be set by the caller.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.scheduled_report import ScheduledReport


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
    cron_by_period = {
        "daily": "0 0 * * *",
        "weekly": "0 0 * * 1",
        "monthly": "0 0 1 * *",
    }
    report = ScheduledReport(
        organisation_id=organisation_id,
        name=f"Cost report: {period} by {group_by}",
        report_type="cost",
        cron_expression=cron_by_period[period],
        config_json={
            "period": period,
            "group_by": group_by,
            "format": format,
            "schedule_type": schedule_type,
        },
        recipient_config={"emails": recipients},
        created_by=account_id,
        next_send_at=next_run_at,
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
    )
    result = await session.execute(q)
    return result.scalar_one_or_none()


async def delete_scheduled_report(
    session: AsyncSession,
    *,
    report_id: uuid.UUID,
    organisation_id: uuid.UUID,
) -> bool:
    q = delete(ScheduledReport).where(
        ScheduledReport.id == report_id,
        ScheduledReport.organisation_id == organisation_id,
    )
    await session.execute(q)
    await session.flush()
    return True
