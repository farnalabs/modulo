"""Analytics query builder — plain SQLAlchemy Core over ``run_daily_facts`` (ADR 020).

Isolation invariant (CRITICAL): ``modulo_app`` is BYPASSRLS and the ORM tenant
filter is NOT registered on Postgres — the explicit ``organisation_id = :org``
predicate injected here is the ONLY isolation control. EVERY statement carries
it; never strip it.

Rules:

- filters are allowlisted keys mapped to bound scalars (enum params, uuid
  params) — NO string interpolation anywhere;
- day/hour-level ``GROUP BY`` (``run_date`` / ``date_trunc('hour', created_at)``);
  ``ORDER BY run_date, run_id``;
- NO ``LIMIT`` before bucketing — limit/order are applied post-bucketing in
  Python (``bucket_rows``);
- week bucketing + zero-fill happen in Python from an explicit day-grid
  (ISO Monday week boundary); hour zero-fill happens from an explicit hour-grid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa

from modulo.db.models.run_daily_facts import RunDailyFact

__all__ = [
    "AnalyticsDimension",
    "AnalyticsGroupBy",
    "AnalyticsQuery",
    "AnalyticsStatus",
    "AnalyticsTriggerType",
    "bucket_rows",
    "build_facts_query",
    "resolve_group_by",
]

_COMPLETE_STATUS = "complete"


class AnalyticsGroupBy(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"


class AnalyticsDimension(StrEnum):
    TRIGGER_TYPE = "trigger_type"
    STATUS = "status"
    PIPELINE = "pipeline"
    FOLDER = "folder"
    TEAM = "team"


class AnalyticsTriggerType(StrEnum):
    MANUAL = "manual"
    WEBHOOK = "webhook"
    CRON = "cron"
    POLLING = "polling"
    AGENT_SIGNAL = "agent_signal"
    CORRECTION = "correction"


class AnalyticsStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    CLAIMED = "claimed"
    WAITING_FOR_LOCK = "waiting_for_lock"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EVAL_FAILED = "eval_failed"


@dataclass(frozen=True)
class AnalyticsQuery:
    """Typed parameters for a facts query — the only values the builder reads."""

    org_id: uuid.UUID
    group_by: AnalyticsGroupBy = AnalyticsGroupBy.DAY
    dimension: AnalyticsDimension | None = None
    trigger_type: AnalyticsTriggerType | None = None
    status: AnalyticsStatus | None = None
    pipeline_id: uuid.UUID | None = None
    folder_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = 1000


# Allowlisted dimension → group column. Keys are enum members only — the dict
# lookup is the allowlist; a non-enum value can never reach here.
_DIMENSION_COLUMNS: dict[AnalyticsDimension, Any] = {
    AnalyticsDimension.TRIGGER_TYPE: RunDailyFact.trigger_type,
    AnalyticsDimension.STATUS: RunDailyFact.status,
    AnalyticsDimension.PIPELINE: RunDailyFact.pipeline_id,
    AnalyticsDimension.FOLDER: RunDailyFact.folder_id,
    AnalyticsDimension.TEAM: RunDailyFact.team_id,
}

# Allowlisted dimension → display-label column (snapshot names). Selected via
# ``MIN`` so the group column alone stays in ``GROUP BY``.
_DIMENSION_LABELS: dict[AnalyticsDimension, Any] = {
    AnalyticsDimension.PIPELINE: RunDailyFact.pipeline_name,
    AnalyticsDimension.TEAM: RunDailyFact.team_name,
}


def build_facts_query(query: AnalyticsQuery) -> tuple[sa.Select[Any], dict[str, Any]]:
    """Build the day-level Core ``select`` + bound params for *query*.

    Returns ``(stmt, params)``. ``params`` carries every bound value; the
    statement is fully parameterised (no string interpolation).
    """
    group_cols: list[Any] = [RunDailyFact.run_date]
    select_cols: list[Any] = [RunDailyFact.run_date]
    if query.group_by == AnalyticsGroupBy.HOUR:
        # Hour buckets truncate the fact's ``created_at`` instant to the hour.
        # Labelled ``run_date`` so ``bucket_rows`` keeps reading ``row.run_date``
        # (the raw UTC-attributed day stays the day-level filter key below).
        time_expr: Any = sa.func.date_trunc("hour", RunDailyFact.created_at).label("run_date")
        group_cols = [time_expr]
        select_cols = [time_expr]
    select_cols += [
        # Complete-run count for success_rate — a FILTER keeps it out of the
        # group key while staying computable at day granularity.
        sa.func.count(RunDailyFact.id).filter(RunDailyFact.status == _COMPLETE_STATUS).label("complete_count"),
        sa.func.count(RunDailyFact.id).label("count"),
        sa.func.sum(RunDailyFact.total_cost_usd).label("total_cost_usd"),
        sa.func.sum(RunDailyFact.total_tokens).label("total_tokens"),
        sa.func.avg(RunDailyFact.duration_ms).label("avg_duration_ms"),
    ]

    params: dict[str, Any] = {"org_id": query.org_id}

    if query.dimension is not None:
        dim_col = _DIMENSION_COLUMNS[query.dimension]
        group_cols.append(dim_col)
        # The raw dimension key must be in the SELECT list, not just GROUP BY —
        # bucket_rows resolves each bucket's key from the row attributes, and
        # without the column in the select the lookup always misses (collapsing
        # every bucket under key=None). This also powers the UUID fallback for
        # PIPELINE/TEAM when the snapshot label is NULL.
        select_cols.append(dim_col)
        label_col = _DIMENSION_LABELS.get(query.dimension)
        if label_col is not None:
            select_cols.append(sa.func.min(label_col).label("key_label"))

    stmt = sa.select(*select_cols).where(RunDailyFact.organisation_id == sa.bindparam("org_id", type_=sa.Uuid))

    if query.date_from is not None:
        params["date_from"] = query.date_from
        stmt = stmt.where(RunDailyFact.run_date >= sa.bindparam("date_from", type_=sa.Date))
    if query.date_to is not None:
        params["date_to"] = query.date_to
        stmt = stmt.where(RunDailyFact.run_date <= sa.bindparam("date_to", type_=sa.Date))

    if query.trigger_type is not None:
        params["trigger_type"] = query.trigger_type.value
        stmt = stmt.where(RunDailyFact.trigger_type == sa.bindparam("trigger_type", type_=sa.String))
    if query.status is not None:
        params["status"] = query.status.value
        stmt = stmt.where(RunDailyFact.status == sa.bindparam("status", type_=sa.String))
    if query.pipeline_id is not None:
        params["pipeline_id"] = query.pipeline_id
        stmt = stmt.where(RunDailyFact.pipeline_id == sa.bindparam("pipeline_id", type_=sa.Uuid))
    if query.folder_id is not None:
        params["folder_id"] = query.folder_id
        stmt = stmt.where(RunDailyFact.folder_id == sa.bindparam("folder_id", type_=sa.Uuid))

    stmt = stmt.group_by(*group_cols).order_by(*group_cols)
    return stmt, params


def _week_start(day: date) -> date:
    """ISO Monday week boundary."""
    return day - timedelta(days=day.weekday())


def _hour_grid(date_from: date, date_to: date) -> list[datetime]:
    """Hour-starts from ``date_from`` 00:00 UTC through ``date_to`` 23:59 UTC."""
    start = datetime.combine(date_from, time.min, tzinfo=UTC)
    end = datetime.combine(date_to, time(23, 59, 59), tzinfo=UTC)
    grid: list[datetime] = []
    cursor = start
    while cursor <= end:
        grid.append(cursor)
        cursor += timedelta(hours=1)
    return grid


def resolve_group_by(
    group_by: AnalyticsGroupBy | None,
    date_from: date | datetime | None,
    date_to: date | datetime | None,
) -> AnalyticsGroupBy:
    """Auto-granularity by range span (``auto_granularity=true``).

    Explicit hour/week choices pass through unchanged. DAY (or None) resolves to
    HOUR for spans <= 3 days, DAY for spans <= 90 days, WEEK otherwise. Without a
    bounded range it stays DAY (backward-compatible default).
    """
    if group_by not in (None, AnalyticsGroupBy.DAY):
        return group_by or AnalyticsGroupBy.DAY
    if date_from is None or date_to is None:
        return AnalyticsGroupBy.DAY
    frm = (
        date_from.replace(tzinfo=UTC)
        if isinstance(date_from, datetime)
        else datetime.combine(date_from, time.min, tzinfo=UTC)
    )
    to = (
        date_to.replace(tzinfo=UTC)
        if isinstance(date_to, datetime)
        else datetime.combine(date_to, time(23, 59, 59), tzinfo=UTC)
    )
    span = (to - frm).days
    if span <= 3:
        return AnalyticsGroupBy.HOUR
    if span <= 90:
        return AnalyticsGroupBy.DAY
    return AnalyticsGroupBy.WEEK


def bucket_rows(
    rows: list[Any],
    *,
    group_by: AnalyticsGroupBy,
    dimension: AnalyticsDimension | None,
    date_from: date,
    date_to: date,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Bucket day-level rows into the response series (backend = sole authority).

    Hour/day/ISO-week buckets, zero-filled from an explicit time grid (zero-fill
    independent of row presence). Hour buckets run from ``date_from`` 00:00 UTC
    to ``date_to`` 23:59 UTC and emit ISO datetimes; day/week buckets emit ISO
    dates. For dimensioned queries each time bucket is repeated per observed
    dimension key. ``limit`` is applied AFTER bucketing (the most recent buckets
    win).
    """
    # Aggregate the day-level rows into (time_key, dim_key) buckets.
    agg: dict[tuple[Any, Any | None], dict[str, Any]] = {}
    for row in rows:
        day = row.run_date
        tkey = _week_start(day) if group_by == AnalyticsGroupBy.WEEK else day
        dkey: Any = None
        if dimension is not None:
            label = getattr(row, "key_label", None)
            raw = label if label is not None else getattr(row, _DIMENSION_KEY_ATTR[dimension], None)
            # Normalize to a comparable string: dimension keys may be UUID ids
            # (folder_id/pipeline_id/team_id fallback when the snapshot label is
            # NULL) or label strings. Never emit a raw UUID — mixing UUID and
            # None in the bucket key crashes `sorted` and breaks the
            # ``str | None`` response model (AnalyticsBucket.key).
            dkey = str(raw) if raw is not None else None
        bkey: tuple[Any, Any | None] = (tkey, dkey)
        bucket = agg.get(bkey)
        if bucket is None:
            bucket = {
                "count": 0,
                "complete": 0,
                "cost": None,
                "tokens": None,
                "duration_sum": 0.0,
                "duration_n": 0,
            }
            agg[bkey] = bucket
        cnt = int(row.count or 0)
        bucket["count"] += cnt
        bucket["complete"] += int(getattr(row, "complete_count", None) or 0)
        if row.total_cost_usd is not None:
            bucket["cost"] = (bucket["cost"] or Decimal(0)) + Decimal(str(row.total_cost_usd))
        if row.total_tokens is not None:
            bucket["tokens"] = (bucket["tokens"] or 0) + int(row.total_tokens)
        if row.avg_duration_ms is not None:
            bucket["duration_sum"] += float(row.avg_duration_ms) * cnt
            bucket["duration_n"] += cnt

    # Explicit time grid: hourly (from date_from 00:00 UTC to date_to 23:59 UTC)
    # for hour grouping, otherwise the day grid (week Mondays for week grouping).
    # Each branch builds a single-typed list so mypy can reconcile the grid type.
    grid_times: list[date] | list[datetime]
    if group_by == AnalyticsGroupBy.HOUR:
        grid_times = sorted(_hour_grid(date_from, date_to))
    else:
        day_from = date_from.date() if isinstance(date_from, datetime) else date_from
        day_to = date_to.date() if isinstance(date_to, datetime) else date_to
        grid_days: list[date] = []
        day = day_from
        while day <= day_to:
            grid_days.append(day)
            day += timedelta(days=1)
        grid_times = sorted({_week_start(d) for d in grid_days}) if group_by == AnalyticsGroupBy.WEEK else grid_days

    dim_keys: list[Any] = [None]
    if dimension is not None:
        # All keys are already normalized to ``str | None``, so the sort is
        # None-safe. An empty range (no observed dimension keys) falls back to
        # ``[None]`` so a dimensioned query still zero-fills the requested grid
        # — same shape as the non-dimensioned case.
        dim_keys = sorted({bk[1] for bk in agg}, key=lambda k: (k is None, k or "")) or [None]

    out: list[dict[str, Any]] = []
    for tkey in grid_times:
        for dkey in dim_keys:
            out_key: tuple[Any, Any | None] = (tkey, dkey)
            b = agg.get(out_key)
            count = b["count"] if b else 0
            complete = b["complete"] if b else 0
            cost = float(b["cost"]) if b and b["cost"] is not None else None
            tokens = b["tokens"] if b else None
            avg_dur = (b["duration_sum"] / b["duration_n"]) if b and b["duration_n"] else None
            success_rate = (complete / count) if count else None
            out.append(
                {
                    "date": tkey.replace(tzinfo=None).isoformat() if isinstance(tkey, datetime) else tkey.isoformat(),
                    "key": dkey,
                    "count": count,
                    "total_cost_usd": cost,
                    "total_tokens": tokens,
                    "avg_duration_ms": round(avg_dur, 1) if avg_dur is not None else None,
                    "success_rate": round(success_rate, 4) if success_rate is not None else None,
                }
            )

    out.sort(key=lambda b: (b["date"], b["key"] or ""))
    if 0 < limit < len(out):
        out = out[-limit:]
    return out


# Dimension → row attribute that carries the raw dimension value on the select
# row (used when no snapshot label exists, e.g. folder_id).
_DIMENSION_KEY_ATTR: dict[AnalyticsDimension, str] = {
    AnalyticsDimension.TRIGGER_TYPE: "trigger_type",
    AnalyticsDimension.STATUS: "status",
    AnalyticsDimension.PIPELINE: "pipeline_id",
    AnalyticsDimension.FOLDER: "folder_id",
    AnalyticsDimension.TEAM: "team_id",
}
