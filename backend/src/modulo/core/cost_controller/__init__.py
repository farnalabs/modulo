"""Cost controller — spend checks, daily run count updates, and limit enforcement.

All functions assume an active transaction with RLS org context set by the caller.
Spend limit checks use SELECT FOR UPDATE for atomicity.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run
from modulo.db.models.team import Team

__all__ = [
    "build_cost_report_buckets",
    "check_and_record_spend",
    "get_cost_report",
    "get_or_create_daily_count",
]

_REPORT_COMPONENT_LIMIT = 500
_REPORT_QUANT = Decimal("0.000001")


def _safe_float(value: Decimal | None) -> float:
    return float(value) if value is not None else 0.0


def _safe_int(value: Decimal | int | None) -> int:
    return int(value) if value is not None else 0


def _report_since(today: date, period: str) -> date:
    """Start-of-period date for the report windows (day/week/month/year)."""
    if period == "day":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return date(today.year, today.month, 1)
    return date(today.year, 1, 1)


def _report_amount(value: Decimal) -> str:
    """Serialize a reporting bucket as a 6dp Decimal string (never float)."""
    try:
        return format(value.quantize(_REPORT_QUANT, rounding=ROUND_HALF_UP), "f")
    except (TypeError, ValueError, ArithmeticError):
        return "0.000000"


async def get_or_create_daily_count(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_date: date,
    team_id: uuid.UUID | None = None,
) -> OrgDailyRunCount:
    """Get today's run count row (org-level or team-level), creating it if missing.

    The caller is expected to SELECT FOR UPDATE on the returned row
    before mutating it (see check_and_record_spend).
    """
    q = (
        select(OrgDailyRunCount)
        .where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date == run_date,
            OrgDailyRunCount.team_id == team_id,
        )
        .with_for_update()
    )

    result = await session.execute(q)
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    savepoint = await session.begin_nested()
    try:
        row = OrgDailyRunCount(
            organisation_id=org_id,
            run_date=run_date,
            team_id=team_id,
            run_count=0,
            total_spend_usd=Decimal(0),
        )
        session.add(row)
        await session.flush()
        return row
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        await savepoint.rollback()
        result = await session.execute(q)
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        raise


async def check_and_record_spend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    cost_usd: Decimal | None,
    team_id: uuid.UUID | None = None,
) -> tuple[bool, str | None]:
    """Check spend limits atomically and record the spend.

    Returns (approved: bool, reason: str | None).
    If the spend would exceed the daily limit, returns (False, "Daily spend limit exceeded").
    Otherwise increments the daily count and returns (True, None).
    """
    if cost_usd is None:
        return False, "Cost must not be None"
    if cost_usd.is_nan() or cost_usd.is_infinite():
        return False, "Cost must be a finite non-negative number"
    if cost_usd < 0:
        return False, "Cost must be non-negative"

    today = datetime.now(UTC).date()

    # Lock and load the org daily count row
    org_count = await get_or_create_daily_count(
        session,
        org_id=org_id,
        run_date=today,
        team_id=None,
    )

    # Lock and load the org spend limit
    org_limit_result = await session.execute(
        select(Organisation.daily_spend_limit)
        .where(
            Organisation.id == org_id,
        )
        .with_for_update()
    )
    org_limit = org_limit_result.scalar_one_or_none()

    new_org_spend = org_count.total_spend_usd + cost_usd
    if org_limit is not None and new_org_spend > org_limit:
        return False, "Daily spend limit exceeded for organisation"

    if team_id is not None:
        team_count = await get_or_create_daily_count(
            session,
            org_id=org_id,
            run_date=today,
            team_id=team_id,
        )

        team_limit_result = await session.execute(
            select(Team.daily_spend_limit)
            .where(
                Team.id == team_id,
            )
            .with_for_update()
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
        q = (
            select(
                OrgDailyRunCount.team_id,
                func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend_usd"),
                func.sum(OrgDailyRunCount.run_count).label("total_runs"),
            )
            .where(
                OrgDailyRunCount.organisation_id == org_id,
                OrgDailyRunCount.run_date >= since,
                OrgDailyRunCount.team_id.isnot(None),
            )
            .group_by(OrgDailyRunCount.team_id)
        )

        result = await session.execute(q)
        rows = result.all()

        team_ids = [row.team_id for row in rows if row.team_id is not None]
        teams_map: dict[uuid.UUID, Team] = {}
        if team_ids:
            teams_result = await session.execute(select(Team).where(Team.id.in_(team_ids)))
            teams_map = {t.id: t for t in teams_result.scalars().all()}

        report = []
        for row in rows:
            if row.team_id is None:
                continue
            team = teams_map.get(row.team_id)
            report.append(
                {
                    "entity_id": str(row.team_id),
                    "entity_name": team.name if team else "Unknown",
                    "total_spend_usd": _safe_float(row.total_spend_usd),
                    "total_runs": _safe_int(row.total_runs),
                }
            )
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
    org_row = result.one_or_none()
    org_spend = _safe_float(org_row.total_spend_usd if org_row else None)
    org_runs = _safe_int(org_row.total_runs if org_row else None)

    org_result = await session.execute(select(Organisation.name).where(Organisation.id == org_id))
    org_name = org_result.scalar_one_or_none() or "Unknown"

    return [
        {
            "entity_id": str(org_id),
            "entity_name": org_name,
            "total_spend_usd": org_spend,
            "total_runs": org_runs,
        }
    ]


async def build_cost_report_buckets(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    period: str = "month",
) -> dict[str, Any]:
    """PR B: per-component + org reporting buckets over the RUNS table.

    REPORTING fields only — the ledger (``OrgDailyRunCount``) stays the
    period-total source; these buckets read the runs table, which IS purged at
    ~90 days by run retention, so they cover only un-purged runs (for a
    year-to-date report, windows > 90 days show empty buckets — accepted and
    stated in the operator guide).

    Returns:
        components_by_team: ``{str(team_id) | "__org__": [{name, amount_usd}]}``
            where ``component`` is the stable aggregation key (pre-delete and
            post-recreate amounts combine under one slug).
        annotations_by_team: ``{str(team_id) | "__org__": {"refused_total_usd",
            "clamped_total_usd"}}`` — refused reads the ``refused_spend_usd``
            ledger column (survives the run purge); clamped is the sum of spend
            over days whose ledger row has ``clamped = true`` and is NOT
            additive with ``total_spend_usd``.
        legacy_total: ``Σ run.total_cost_usd`` over scoped runs with
            ``cost_breakdown IS NULL`` (Decimal string).
        org_unassigned_components: component-attributed spend from runs with no
            team (Decimal string).
        org_total: ``Σ(team components) + org_unassigned_components +
            legacy_total`` over NON-marker-bearing runs (Decimal string) — the
            REPORTING invariant, never a health gate.
        has_more: True when a component bucket was truncated at
            ``_REPORT_COMPONENT_LIMIT`` (bounded by design).
    """
    valid_periods = frozenset({"day", "week", "month", "year"})
    if period not in valid_periods:
        raise ValueError(f"Unknown period '{period}'. Expected one of: {', '.join(sorted(valid_periods))}")

    since = _report_since(datetime.now(UTC).date(), period)

    run_result = await session.execute(
        select(Run.owner_team_id, Run.total_cost_usd, Run.cost_breakdown).where(
            Run.organisation_id == org_id,
            Run.started_at.isnot(None),
            Run.started_at >= since,
        )
    )
    run_rows = run_result.all()

    team_components: dict[uuid.UUID | None, dict[str, Decimal]] = {}
    legacy_total = Decimal(0)

    for row in run_rows:
        team_id = row.owner_team_id
        breakdown = row.cost_breakdown
        if breakdown is None:
            try:
                if row.total_cost_usd is not None:
                    legacy_total += Decimal(str(row.total_cost_usd))
            except (TypeError, ValueError, ArithmeticError):
                continue
            continue
        if not isinstance(breakdown, list):
            continue
        # Marker-bearing runs (total flat-clamped to column capacity) are
        # EXCLUDED so the reporting invariant holds exactly.
        if any(isinstance(e, dict) and e.get("total_clamped") is True for e in breakdown):
            continue
        bucket = team_components.setdefault(team_id, {})
        for entry in breakdown:
            if not isinstance(entry, dict):
                continue
            name = entry.get("component")
            if not isinstance(name, str) or not name:
                continue
            try:
                raw_amount = entry.get("amount_usd")
                amount = Decimal(str(raw_amount)) if raw_amount is not None else Decimal(0)
            except (TypeError, ValueError, ArithmeticError):
                continue
            bucket[name] = bucket.get(name, Decimal(0)) + amount

    def _serialized(
        bucket: dict[str, Decimal],
        limit: int = _REPORT_COMPONENT_LIMIT,
    ) -> tuple[list[dict[str, str]], bool]:
        entries = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))
        truncated = len(entries) > limit
        return [{"name": name, "amount_usd": _report_amount(amount)} for name, amount in entries[:limit]], truncated

    components_by_team: dict[str, list[dict[str, str]]] = {}
    has_more = False
    for team_id, bucket in team_components.items():
        key = str(team_id) if team_id is not None else "__org__"
        comps, truncated = _serialized(bucket)
        components_by_team[key] = comps
        if truncated:
            has_more = True

    annotation_result = await session.execute(
        select(
            OrgDailyRunCount.team_id,
            func.sum(OrgDailyRunCount.refused_spend_usd).label("refused_total"),
            func.sum(
                case(
                    (OrgDailyRunCount.clamped.is_(True), OrgDailyRunCount.total_spend_usd),
                    else_=Decimal(0),
                )
            ).label("clamped_total"),
        )
        .where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date >= since,
        )
        .group_by(OrgDailyRunCount.team_id)
    )
    annotations_by_team: dict[str, dict[str, float | None]] = {}
    for row in annotation_result.all():
        key = str(row.team_id) if row.team_id is not None else "__org__"
        refused = _safe_float(row.refused_total)
        clamped = _safe_float(row.clamped_total)
        annotations_by_team[key] = {
            "refused_total_usd": refused if refused > 0 else None,
            "clamped_total_usd": clamped if clamped > 0 else None,
        }

    org_unassigned = Decimal(0)
    team_sum = Decimal(0)
    for team_id, bucket in team_components.items():
        for amount in bucket.values():
            if team_id is None:
                org_unassigned += amount
            else:
                team_sum += amount

    return {
        "components_by_team": components_by_team,
        "annotations_by_team": annotations_by_team,
        "legacy_total": _report_amount(legacy_total),
        "org_unassigned_components": _report_amount(org_unassigned),
        "org_total": _report_amount(team_sum + org_unassigned + legacy_total),
        "has_more": has_more,
    }
