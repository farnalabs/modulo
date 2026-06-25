"""Cost controller — spend checks, daily run count updates, and limit enforcement.

All functions assume an active transaction with RLS org context set by the caller.
Spend limit checks use SELECT FOR UPDATE for atomicity.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.organisation import Organisation
from modulo.db.models.team import Team


async def get_or_create_daily_count(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_date: date,
    team_id: uuid.UUID | None = None,
) -> OrgDailyRunCount:
    """Get today's run count row (org-level or team-level), creating it if missing.

    Uses SELECT FOR UPDATE to block concurrent spend operations.
    """
    q = select(OrgDailyRunCount).where(
        OrgDailyRunCount.organisation_id == org_id,
        OrgDailyRunCount.run_date == run_date,
        OrgDailyRunCount.team_id == team_id,
    ).with_for_update()

    result = await session.execute(q)
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    row = OrgDailyRunCount(
        organisation_id=org_id,
        run_date=run_date,
        team_id=team_id,
        run_count=0,
        total_spend_usd=Decimal("0"),
    )
    session.add(row)
    await session.flush()

    q = select(OrgDailyRunCount).where(
        OrgDailyRunCount.id == row.id,
    ).with_for_update()
    await session.execute(q)
    return row


async def check_and_record_spend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    cost_usd: Decimal,
    team_id: uuid.UUID | None = None,
) -> tuple[bool, str | None]:
    """Check spend limits atomically and record the spend.

    Returns (approved: bool, reason: str | None).
    If the spend would exceed the daily limit, returns (False, "Daily spend limit exceeded").
    Otherwise increments the daily count and returns (True, None).
    """
    today = datetime.now(UTC).date()

    # Lock and load the org daily count row
    org_count = await get_or_create_daily_count(
        session, org_id=org_id, run_date=today, team_id=None,
    )

    # Lock and load the org spend limit
    org_limit_result = await session.execute(
        select(Organisation.daily_spend_limit).where(
            Organisation.id == org_id,
        ).with_for_update()
    )
    org_limit = org_limit_result.scalar_one_or_none()

    new_org_spend = org_count.total_spend_usd + cost_usd
    if org_limit is not None and new_org_spend > org_limit:
        return False, "Daily spend limit exceeded for organisation"

    if team_id is not None:
        team_count = await get_or_create_daily_count(
            session, org_id=org_id, run_date=today, team_id=team_id,
        )

        team_limit_result = await session.execute(
            select(Team.daily_spend_limit).where(
                Team.id == team_id,
            ).with_for_update()
        )
        team_limit = team_limit_result.scalar_one_or_none()

        new_team_spend = team_count.total_spend_usd + cost_usd
        if team_limit is not None and new_team_spend > team_limit:
            return False, "Daily spend limit exceeded for team"

        team_count.total_spend_usd = new_team_spend
        team_count.run_count = team_count.run_count + 1

    org_count.total_spend_usd = new_org_spend
    org_count.run_count = org_count.run_count + 1

    await session.flush()
    return True, None


async def get_cost_report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    group_by: str = "team",
    period: str = "month",
) -> list[dict[str, Any]]:
    """Build a cost report grouped by team or organisation for a given period.

    Args:
        group_by: "team" or "org"
        period: "day", "week", "month", "year"

    Returns a list of dicts with keys: entity_id, entity_name, total_spend_usd, total_runs.
    """
    valid_periods = frozenset({"day", "week", "month", "year"})
    if period not in valid_periods:
        raise ValueError(f"Unknown period '{period}'. Expected one of: {', '.join(sorted(valid_periods))}")

    if group_by not in ("team", "org"):
        raise ValueError(f"Unknown group_by '{group_by}'. Expected 'team' or 'org'.")

    today = datetime.now(UTC).date()

    if period == "day":
        since: date = today
    elif period == "week":
        since = today - timedelta(days=today.weekday())
    elif period == "month":
        since = date(today.year, today.month, 1)
    else:
        since = date(today.year, 1, 1)

    if group_by == "team":
        q = select(
            OrgDailyRunCount.team_id,
            func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend_usd"),
            func.sum(OrgDailyRunCount.run_count).label("total_runs"),
        ).where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date >= since,
            OrgDailyRunCount.team_id.isnot(None),
        ).group_by(OrgDailyRunCount.team_id)

        result = await session.execute(q)
        rows = result.all()

        team_ids = [row.team_id for row in rows]
        teams_result = await session.execute(
            select(Team).where(Team.id.in_(team_ids))
        )
        teams_map = {t.id: t for t in teams_result.scalars().all()}

        report = []
        for row in rows:
            team = teams_map.get(row.team_id)
            report.append({
                "entity_id": str(row.team_id),
                "entity_name": team.name if team else "Unknown",
                "total_spend_usd": float(row.total_spend_usd) if row.total_spend_usd is not None else 0.0,
                "total_runs": int(row.total_runs) if row.total_runs is not None else 0,
            })
        return report

    # group_by == "org"
    org_q = select(
        func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend_usd"),
        func.sum(OrgDailyRunCount.run_count).label("total_runs"),
    ).where(
        OrgDailyRunCount.organisation_id == org_id,
        OrgDailyRunCount.run_date >= since,
        OrgDailyRunCount.team_id.is_(None),
    )
    result = await session.execute(org_q)
    row = result.one()

    org_result = await session.execute(
        select(Organisation.name).where(Organisation.id == org_id)
    )
    org_name = org_result.scalar_one_or_none() or "Unknown"

    return [{
        "entity_id": str(org_id),
        "entity_name": org_name,
        "total_spend_usd": float(row.total_spend_usd) if row.total_spend_usd else 0.0,
        "total_runs": int(row.total_runs) if row.total_runs else 0,
    }]
