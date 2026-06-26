"""CRUD for OrgDailyRunCount records.

All functions require RLS org context to be set by the caller.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.daily_run_count import OrgDailyRunCount

_UNSET = object()


async def upsert_daily_run_count(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_date: date | None = None,
    team_id: uuid.UUID | None = None,
    increment_count: int = 1,
    increment_spend: Decimal = Decimal("0"),
) -> OrgDailyRunCount:
    """Upsert a daily run count row, atomically incrementing counts.

    Does NOT use SELECT FOR UPDATE — for that, use cost_controller.
    This is a convenience wrapper for non-critical updates (e.g. batch backfill).
    """
    if run_date is None:
        run_date = datetime.now(UTC).date()

    q = select(OrgDailyRunCount).where(
        OrgDailyRunCount.organisation_id == org_id,
        OrgDailyRunCount.run_date == run_date,
        OrgDailyRunCount.team_id == team_id,
    )
    result = await session.execute(q)
    row = result.scalar_one_or_none()

    if row is None:
        row = OrgDailyRunCount(
            organisation_id=org_id,
            run_date=run_date,
            team_id=team_id,
            run_count=0,
            total_spend_usd=Decimal("0"),
        )
        session.add(row)

    row.run_count = row.run_count + increment_count
    row.total_spend_usd = row.total_spend_usd + increment_spend
    await session.flush()
    return row


async def get_daily_run_counts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID | None | object = _UNSET,
    since: date | None = None,
    until: date | None = None,
) -> list[OrgDailyRunCount]:
    """List daily run counts for an org, optionally filtered by team and date range.

    Pass team_id=None to filter for org-level rows only (team_id IS NULL).
    Omit team_id entirely to return all rows for the org.
    """
    q = select(OrgDailyRunCount).where(
        OrgDailyRunCount.organisation_id == org_id,
    )
    if team_id is _UNSET:
        pass  # No team filter — return all rows
    elif team_id is None:
        q = q.where(OrgDailyRunCount.team_id.is_(None))  # Org-level rows only
    else:
        q = q.where(OrgDailyRunCount.team_id == team_id)
    if since is not None:
        q = q.where(OrgDailyRunCount.run_date >= since)
    if until is not None:
        q = q.where(OrgDailyRunCount.run_date <= until)

    result = await session.execute(q.order_by(OrgDailyRunCount.run_date.desc()))
    return list(result.scalars().all())


async def get_org_spend_total(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    since: date | None = None,
) -> Decimal:
    """Get the total spend for an org (excluding team-scoped rows) in a period."""
    q = select(func.sum(OrgDailyRunCount.total_spend_usd)).where(
        OrgDailyRunCount.organisation_id == org_id,
        OrgDailyRunCount.team_id.is_(None),
    )
    if since is not None:
        q = q.where(OrgDailyRunCount.run_date >= since)

    result = await session.execute(q)
    return result.scalar_one() or Decimal("0")
