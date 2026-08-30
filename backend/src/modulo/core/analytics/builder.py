"""Analytics query builder — plain SQLAlchemy Core over ``run_daily_facts`` (ADR 020).

Isolation invariant (CRITICAL): ``modulo_app`` is NOBYPASSRLS (tenant isolation
relies on RLS policies) and the ORM tenant
filter is NOT registered on Postgres — the explicit ``organisation_id = :org``
predicate injected here is the PRIMARY isolation control (RLS also enforces org
scoping). EVERY statement carries
it; never strip it.

Rules:

- filters are allowlisted keys mapped to bound scalars (enum params, uuid
  params) — NO string interpolation anywhere;
- day/hour-level ``GROUP BY`` (``run_date`` / ``date_trunc('hour', created_at)``);
  ``ORDER BY run_date, run_id``;
- NO ``LIMIT`` before bucketing for the bucketed query — limit/order are applied
  post-bucketing in Python (``bucket_rows``). The concurrency query is the one
  exception: it applies a fixed raw-row cap (``CONCURRENCY_MAX_RAW_ROWS + 1``)
  so the DB never streams an unbounded fact scan into the app process — overflow
  is detected and rejected as a validation error, never silently truncated;
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

from modulo.core.pipeline_engine.error_codes import (
    expand_code_variants,
    known_error_codes,
    map_legacy_code,
)
from modulo.db.crud.team_scope import team_scope_clause
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run_daily_facts import RunDailyFact

__all__ = [
    "CONCURRENCY_MAX_RAW_ROWS",
    "HOUR_GROUPBY_MAX_RANGE_DAYS",
    "STALL_ERROR_CODES",
    "AnalyticsDimension",
    "AnalyticsGroupBy",
    "AnalyticsQuery",
    "AnalyticsStatus",
    "AnalyticsTriggerType",
    "bucket_concurrency_rows",
    "bucket_rows",
    "build_concurrency_query",
    "build_error_code_condition",
    "build_facts_query",
    "hour_groupby_span_exceeds",
    "resolve_group_by",
    "to_utc_aware",
]

_COMPLETE_STATUS = "complete"
_FAILED_STATUS = "failed"
_STALLED_STATUS = "stalled"

# Error codes that mark a failed run as a STALL — the run made no progress
# (no node dispatched, a node exceeded its wall-clock timeout, or a sandbox
# agent went silent past the idle watchdog). Mirrors the timeout paths that set
# ``Run.error_code``: ``executor_stalled`` (pipeline_execution.EXECUTOR_STALLED
# zombie watchdog), ``node_timeout`` (executor._stream_graph catching
# ``TimeoutError``), and ``TimeoutError`` itself (the generic ``except
# Exception`` fallback in executor.py when the sandbox idle watchdog surfaces
# the class name directly).
STALL_ERROR_CODES: frozenset[str] = frozenset({"executor_stalled", "node_timeout", "TimeoutError"})

# Hour-granularity range cap: an EXPLICIT ``group_by=hour`` over a wider span
# would materialise up to 24 buckets/day per dimension key before limit
# truncation (hour-grid amplification). ``auto_granularity`` never selects hour
# for spans over 3 days, so this only constrains an explicit hour choice.
HOUR_GROUPBY_MAX_RANGE_DAYS = 14

# Raw-row cap for the concurrency query (FAR-134 follow-up): the overlap math
# runs in Python over the raw fact instants, so an unbounded 365-day scan could
# pull hundreds of thousands to millions of rows into the app process. 100k rows
# bounds a full-year scan for all but the busiest orgs (which should narrow the
# range or add pipeline/status filters) while keeping the Python overlap sweep
# inside the statement-timeout budget. The cap degrades to a CLEAR validation
# error — never silent truncation — so max_active / avg_active can never be
# computed from a partial scan.
CONCURRENCY_MAX_RAW_ROWS = 100_000


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
    ERROR_CODE = "error_code"


class AnalyticsTriggerType(StrEnum):
    MANUAL = "manual"
    WEBHOOK = "webhook"
    CRON = "cron"
    POLLING = "polling"
    AGENT_SIGNAL = "agent_signal"
    ONGOING = "ongoing"
    CORRECTION = "correction"


class AnalyticsStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    CLAIMED = "claimed"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EVAL_FAILED = "eval_failed"
    STALLED = "stalled"
    BUDGET_EXCEEDED = "budget_exceeded"
    ROUTER_NO_MATCH = "router_no_match"


@dataclass(frozen=True)
class AnalyticsQuery:
    """Typed parameters for a facts query — the only values the builder reads."""

    org_id: uuid.UUID
    group_by: AnalyticsGroupBy = AnalyticsGroupBy.DAY
    dimension: AnalyticsDimension | None = None
    trigger_type: AnalyticsTriggerType | None = None
    status: AnalyticsStatus | None = None
    pipeline_ids: tuple[uuid.UUID, ...] = ()
    team_id: uuid.UUID | None = None
    error_code: str | None = None
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
    AnalyticsDimension.ERROR_CODE: RunDailyFact.error_code,
}

# Allowlisted dimension → display-label column (snapshot names). Selected via
# ``MIN`` so the group column alone stays in ``GROUP BY``.
_DIMENSION_LABELS: dict[AnalyticsDimension, Any] = {
    AnalyticsDimension.PIPELINE: RunDailyFact.pipeline_name,
    AnalyticsDimension.TEAM: RunDailyFact.team_name,
}


def build_error_code_condition(bind: dict[str, Any], error_code: str) -> Any:
    """WHERE condition for the error-code filter; fills *bind* with the bound list.

    The facts table stores the RAW DB code while the API presents dotted codes,
    so the specific-code path matches every spelling of the same canonical code
    via :func:`expand_code_variants` (an IN-clause).

    The ``harness.unknown`` aggregate ("Unknown error" slice) is the exception:
    ``bucket_rows`` canonicalizes every unmapped raw code through
    :func:`map_legacy_code` into that slice, but the facts table never stores
    the literal dotted ``harness.unknown`` — so an IN-clause over the literal
    matches ZERO rows while the chart shows a populated slice. The aggregate
    filter must match exactly the raw rows the slice shows: every raw code NOT
    in :func:`known_error_codes` (a NOT IN over the complement). NULL
    ``error_code`` rows are NOT in the slice — ``bucket_rows`` sends raw-None
    to a separate ``None`` key — and SQL ``NOT IN`` excludes NULLs naturally,
    so no NULL handling is needed. A raw literal ``harness.unknown`` row IS in
    the slice (registry passthrough), so it must NOT be excluded: subtract it
    from the exclude set. A specific unmapped input (e.g. ``"SomeMysteryError"``)
    keeps the IN-clause behaviour and matches only its own literal rows, never
    the whole unknown slice.
    """
    if error_code == "harness.unknown":
        bind["error_codes"] = sorted(known_error_codes() - {"harness.unknown"})
        return RunDailyFact.error_code.notin_(sa.bindparam("error_codes", type_=sa.String, expanding=True))
    bind["error_codes"] = sorted(expand_code_variants(error_code))
    return RunDailyFact.error_code.in_(sa.bindparam("error_codes", type_=sa.String, expanding=True))


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
    # FAR-102 stall-dimension metrics — all bound, never interpolated.
    # A stalled run is a failure for rate purposes (it never completed).
    failure_status = sa.or_(
        RunDailyFact.status == _FAILED_STATUS,
        RunDailyFact.status == _STALLED_STATUS,
    )
    select_cols += [
        # Complete-run count for success_rate — a FILTER keeps it out of the
        # group key while staying computable at day granularity.
        sa.func.count(RunDailyFact.id).filter(RunDailyFact.status == _COMPLETE_STATUS).label("complete_count"),
        sa.func.count(RunDailyFact.id).label("count"),
        sa.func.sum(RunDailyFact.total_cost_usd).label("total_cost_usd"),
        sa.func.sum(RunDailyFact.total_tokens).label("total_tokens"),
        sa.func.avg(RunDailyFact.duration_ms).label("avg_duration_ms"),
        sa.func.count(RunDailyFact.id).filter(failure_status).label("failure_count"),
        sa.func.count(RunDailyFact.id)
        .filter(
            sa.and_(
                failure_status,
                RunDailyFact.error_code.in_(sa.bindparam("stall_error_codes", type_=sa.String, expanding=True)),
            )
        )
        .label("stall_count"),
        sa.func.avg(RunDailyFact.queue_wait_ms).label("avg_queue_wait_ms"),
        sa.func.avg(RunDailyFact.final_idle_ms).label("avg_final_idle_ms"),
        sa.func.avg(RunDailyFact.output_bytes).label("avg_output_bytes"),
    ]

    params: dict[str, Any] = {
        "org_id": query.org_id,
        "stall_error_codes": sorted(STALL_ERROR_CODES),
    }

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
    if query.pipeline_ids:
        params["pipeline_ids"] = list(query.pipeline_ids)
        stmt = stmt.where(RunDailyFact.pipeline_id.in_(sa.bindparam("pipeline_ids", type_=sa.Uuid, expanding=True)))
    if query.team_id is not None:
        # A team-scoped caller sees its own team's facts plus org-level facts
        # (no owner team) — the same boundary the MCP guard applies. The fact's
        # stamped team is the source of truth; facts predating the create-time
        # run stamp (NULL) fall back to the pipeline's owner so a NULL stamp
        # can never widen the boundary.
        params["team_id"] = query.team_id
        stmt = stmt.outerjoin(Pipeline, Pipeline.id == RunDailyFact.pipeline_id)
        effective_team = sa.func.coalesce(RunDailyFact.team_id, Pipeline.owner_team_id)
        stmt = stmt.where(team_scope_clause(effective_team, sa.bindparam("team_id", type_=sa.Uuid)))
    if query.error_code is not None:
        stmt = stmt.where(build_error_code_condition(params, query.error_code))
    if query.folder_id is not None:
        params["folder_id"] = query.folder_id
        stmt = stmt.where(RunDailyFact.folder_id == sa.bindparam("folder_id", type_=sa.Uuid))

    stmt = stmt.group_by(*group_cols).order_by(*group_cols)
    return stmt, params


# ---------------------------------------------------------------------------
# Concurrency / slot-utilization query (FAR-134)
# ---------------------------------------------------------------------------
#
# The facts table stores absolute run instants (created_at, started_at,
# completed_at) but NO per-instant run state, so "how many runs were running /
# queued at any instant" cannot be reconstructed with a GROUP BY. The SQL side
# therefore selects the RAW instants over the range + filters (org predicate
# intact — the PRIMARY isolation control, with RLS also enforcing org scoping)
# and the overlap math happens in Python
# (``bucket_concurrency_rows``), exactly like ``bucket_rows`` does zero-fill.
# The raw scan is bounded by ``CONCURRENCY_MAX_RAW_ROWS + 1`` (cap + detection
# sentinel): the service rejects the query when the scan exceeds the cap rather
# than silently truncating, so bucket maxima/averages stay exact.


def build_concurrency_query(query: AnalyticsQuery) -> tuple[sa.Select[Any], dict[str, Any]]:
    """Select the raw concurrency inputs (run instants) over the range + filters.

    Returns ``(stmt, params)`` — fully parameterised, carrying the org
    predicate and the same allowlisted bound filters as ``build_facts_query``,
    but with NO ``GROUP BY``: the overlap counting is done per-bucket in Python.
    The statement is bounded by ``CONCURRENCY_MAX_RAW_ROWS + 1`` rows (cap +
    detection sentinel) so the DB never streams an unbounded scan into Python;
    the caller rejects overflow as a validation error instead of truncating.
    """
    select_cols: list[Any] = [
        RunDailyFact.run_date,
        RunDailyFact.created_at,
        RunDailyFact.started_at,
        RunDailyFact.completed_at,
    ]
    params: dict[str, Any] = {
        "org_id": query.org_id,
    }
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
    if query.pipeline_ids:
        params["pipeline_ids"] = list(query.pipeline_ids)
        stmt = stmt.where(RunDailyFact.pipeline_id.in_(sa.bindparam("pipeline_ids", type_=sa.Uuid, expanding=True)))
    if query.team_id is not None:
        # A team-scoped caller sees its own team's facts plus org-level facts
        # (no owner team) — the same boundary the MCP guard applies. The fact's
        # stamped team is the source of truth; facts predating the create-time
        # run stamp (NULL) fall back to the pipeline's owner so a NULL stamp
        # can never widen the boundary.
        params["team_id"] = query.team_id
        stmt = stmt.outerjoin(Pipeline, Pipeline.id == RunDailyFact.pipeline_id)
        effective_team = sa.func.coalesce(RunDailyFact.team_id, Pipeline.owner_team_id)
        stmt = stmt.where(team_scope_clause(effective_team, sa.bindparam("team_id", type_=sa.Uuid)))
    if query.error_code is not None:
        stmt = stmt.where(build_error_code_condition(params, query.error_code))
    if query.folder_id is not None:
        params["folder_id"] = query.folder_id
        stmt = stmt.where(RunDailyFact.folder_id == sa.bindparam("folder_id", type_=sa.Uuid))

    stmt = stmt.order_by(RunDailyFact.run_date)
    # Bounded raw-row cap: the DB returns at most cap+1 rows (cap + detection
    # sentinel). The service rejects the query when the scan exceeds the cap —
    # NEVER truncates — so the Python overlap sweep stays exact and bounded.
    stmt = stmt.limit(CONCURRENCY_MAX_RAW_ROWS + 1)
    return stmt, params


def _normalised_instants(rows: list[Any]) -> list[tuple[datetime | None, datetime | None, datetime | None]]:
    """Normalise each row's (created_at, started_at, completed_at) to aware UTC.

    Done ONCE up front so the per-bucket sweep never re-normalises. Naive
    datetimes are treated as UTC; non-UTC offsets are converted.
    """
    out: list[tuple[datetime | None, datetime | None, datetime | None]] = []
    for row in rows:
        created = getattr(row, "created_at", None)
        started = getattr(row, "started_at", None)
        completed = getattr(row, "completed_at", None)
        out.append(
            (
                to_utc_aware(created) if created is not None else None,
                to_utc_aware(started) if started is not None else None,
                to_utc_aware(completed) if completed is not None else None,
            )
        )
    return out


def _active_interval(
    started_at: datetime | None,
    completed_at: datetime | None,
    bucket_start: datetime,
    bucket_end: datetime,
) -> tuple[datetime, datetime] | None:
    """The portion of the run's ``[started_at, completed_at)`` inside the bucket.

    ``None`` when the run is not active at any instant in the bucket. A run
    spanning a bucket boundary is clamped into the bucket (``max(start,
    bucket_start)`` .. ``min(completed, bucket_end)``), so it contributes to
    BOTH adjacent buckets. A run with ``completed_at`` NULL is treated as
    open-ended — active through the end of the bucket.
    """
    if started_at is None:
        return None
    if completed_at is not None and completed_at <= bucket_start:
        return None  # finished before the bucket opened
    if started_at >= bucket_end:
        return None  # has not started within the bucket
    start = max(started_at, bucket_start)
    end = completed_at if completed_at is not None and completed_at < bucket_end else bucket_end
    if start >= end:
        return None
    return (start, end)


def _queued_interval(
    created_at: datetime | None,
    started_at: datetime | None,
    bucket_start: datetime,
    bucket_end: datetime,
) -> tuple[datetime, datetime] | None:
    """The portion of the run's queue wait (``created_at``..``started_at``) inside the bucket.

    A run counts as queued at instant ``t`` when ``created_at <= t <
    started_at``. ``started_at`` NULL (never started) counts as queued through
    the end of the bucket. A run created in an earlier bucket but started in
    this one contributes ``bucket_start``..``started_at`` here.
    """
    if created_at is None or created_at >= bucket_end:
        return None  # not yet created within the bucket
    if started_at is not None and started_at <= bucket_start:
        return None  # already started before the bucket opened — not queued here
    start = max(created_at, bucket_start)
    end = started_at if started_at is not None and started_at < bucket_end else bucket_end
    if start >= end:
        return None
    return (start, end)


def _sweep_peak_and_mean(
    intervals: list[tuple[datetime, datetime]],
    bucket_start: datetime,
    bucket_end: datetime,
) -> tuple[int, float]:
    """Line-sweep ``[start, end)`` intervals: peak count + time-weighted mean.

    Events sort so an end (-1) at instant ``t`` precedes a start (+1) at the
    same instant — matching the half-open interval semantics (an interval
    ending at ``t`` does NOT cover ``t``; one starting at ``t`` does). Returns
    ``(max_concurrent, time-weighted-mean-across-the-bucket)`` — the mean is
    the exact limit of the sampled mean, i.e. "sum of overlap seconds / bucket
    seconds".
    """
    if not intervals:
        return 0, 0.0
    events: list[tuple[datetime, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    current = 0
    peak = 0
    weighted = 0.0
    prev = bucket_start
    for ts, delta in events:
        if ts > prev:
            weighted += current * (ts - prev).total_seconds()
            prev = ts
        current += delta
        peak = max(peak, current)
    if bucket_end > prev:
        weighted += current * (bucket_end - prev).total_seconds()
    total = (bucket_end - bucket_start).total_seconds()
    return peak, weighted / total if total > 0 else 0.0


def _concurrency_bucket_grid(
    group_by: AnalyticsGroupBy,
    date_from: date | datetime,
    date_to: date | datetime,
) -> list[tuple[datetime, datetime, str]]:
    """``(bucket_start, bucket_end, label)`` per bucket over the range.

    Hour buckets run from the hour boundary of ``date_from`` through ``date_to``
    23:59:59 and label as ISO datetimes; day/week buckets label as ISO dates
    (week buckets ISO-Monday-anchored) — mirroring ``bucket_rows``.
    """
    frm = to_utc_aware(date_from)
    to = to_utc_aware(date_to, end_of_day=True)
    buckets: list[tuple[datetime, datetime, str]] = []
    if group_by == AnalyticsGroupBy.HOUR:
        cursor = datetime(frm.year, frm.month, frm.day, frm.hour, tzinfo=UTC)
        while cursor < to:
            end = cursor + timedelta(hours=1)
            buckets.append((cursor, end, cursor.replace(tzinfo=None).isoformat()))
            cursor = end
        return buckets
    if group_by == AnalyticsGroupBy.WEEK:
        cursor = datetime.combine(_week_start(frm.date()), time.min, tzinfo=UTC)
        while cursor <= to:
            end = cursor + timedelta(days=7)
            buckets.append((cursor, end, cursor.date().isoformat()))
            cursor = end
        return buckets
    cursor = datetime.combine(frm.date(), time.min, tzinfo=UTC)
    while cursor <= to:
        end = cursor + timedelta(days=1)
        buckets.append((cursor, end, cursor.date().isoformat()))
        cursor = end
    return buckets


def bucket_concurrency_rows(
    rows: list[Any],
    *,
    group_by: AnalyticsGroupBy,
    date_from: date | datetime,
    date_to: date | datetime,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Bucket raw concurrency rows into the slot-utilization series (FAR-134).

    The SQL side only selects raw instants; the overlap math lives HERE, like
    ``bucket_rows`` owns zero-fill. The input is bounded by
    ``CONCURRENCY_MAX_RAW_ROWS`` (the statement carries a cap+1 sentinel limit);
    the service rejects overflow before this function runs, so the sweep never
    processes a truncated scan. Per bucket the timeline is line-swept over
    interval start/end events, giving EXACT peak and time-weighted-mean counts:

    - ``max_active`` / ``avg_active``: concurrent runs whose ``[started_at,
      completed_at)`` overlaps the bucket. A run spanning a bucket boundary
      contributes to BOTH buckets. A never-completed run (``completed_at``
      NULL) counts as active through the bucket's end.
    - ``max_queued`` / ``avg_queued``: runs waiting for a slot at some instant
      in the bucket (``created_at <= t < started_at``); never-started runs
      (``started_at`` NULL) count as queued through the bucket's end.
    """
    instants = _normalised_instants(rows)
    out: list[dict[str, Any]] = []
    for bucket_start, bucket_end, label in _concurrency_bucket_grid(group_by, date_from, date_to):
        active_intervals: list[tuple[datetime, datetime]] = []
        queued_intervals: list[tuple[datetime, datetime]] = []
        for created_at, started_at, completed_at in instants:
            active = _active_interval(started_at, completed_at, bucket_start, bucket_end)
            if active is not None:
                active_intervals.append(active)
            queued = _queued_interval(created_at, started_at, bucket_start, bucket_end)
            if queued is not None:
                queued_intervals.append(queued)
        max_active, avg_active = _sweep_peak_and_mean(active_intervals, bucket_start, bucket_end)
        max_queued, avg_queued = _sweep_peak_and_mean(queued_intervals, bucket_start, bucket_end)
        out.append(
            {
                "date": label,
                "max_active": max_active,
                "avg_active": round(avg_active, 2),
                "max_queued": max_queued,
                "avg_queued": round(avg_queued, 2),
            }
        )
    if 0 < limit < len(out):
        out = out[-limit:]
    return out


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


def to_utc_aware(value: date | datetime, *, end_of_day: bool = False) -> datetime:
    """Normalise a date/datetime to an aware UTC instant.

    Naive datetimes are treated as UTC (``tzinfo=UTC``); aware datetimes with a
    NON-UTC offset are CONVERTED to UTC via ``.astimezone(UTC)`` — never
    re-labelled, so ``2026-08-06T14:00:00+05:00`` buckets/labels from the
    UTC-converted instant (09:00Z). Bare dates expand to 00:00 UTC (or 23:59:59
    with ``end_of_day``) so an hour grid covers the whole day. A datetime at
    exactly midnight (the shape FastAPI produces for a date-only query param
    like ``?date_to=2026-08-11``) is treated the same as a bare date with
    ``end_of_day`` — otherwise a single-day hour query collapses to zero
    buckets. Datetimes carrying a real time-of-day are never expanded.
    """
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if end_of_day and aware.hour == 0 and aware.minute == 0 and aware.second == 0 and aware.microsecond == 0:
            return aware.replace(hour=23, minute=59, second=59, microsecond=0)
        return aware
    return datetime.combine(value, time(23, 59, 59) if end_of_day else time.min, tzinfo=UTC)


def hour_groupby_span_exceeds(
    date_from: date | datetime,
    date_to: date | datetime,
    *,
    max_days: int = HOUR_GROUPBY_MAX_RANGE_DAYS,
) -> bool:
    """True when the effective range spans more than *max_days* days.

    Guard for hour-granularity bucket amplification. ``auto_granularity`` never
    selects hour for wide ranges, so this only fires on an EXPLICIT
    ``group_by=hour`` over a wide range. Both bounds are normalised to aware UTC
    before the span arithmetic, so mixed naive/aware inputs are safe.
    """
    frm = to_utc_aware(date_from)
    to = to_utc_aware(date_to, end_of_day=True)
    return (to - frm).days > max_days


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
    frm = to_utc_aware(date_from)
    to = to_utc_aware(date_to, end_of_day=True)
    span = (to - frm).days
    if span <= 3:
        return AnalyticsGroupBy.HOUR
    if span <= 90:
        return AnalyticsGroupBy.DAY
    return AnalyticsGroupBy.WEEK


def _empty_bucket() -> dict[str, Any]:
    return {
        "count": 0,
        "complete": 0,
        "cost": None,
        "tokens": None,
        "duration_sum": 0.0,
        "duration_n": 0,
        "failure": 0,
        "stall": 0,
        "queue_wait_sum": 0.0,
        "queue_wait_n": 0,
        "final_idle_sum": 0.0,
        "final_idle_n": 0,
        "output_bytes_sum": 0.0,
        "output_bytes_n": 0,
    }


def _row_dimension_key(row: Any, dimension: AnalyticsDimension | None) -> Any | None:
    if dimension is None:
        return None
    label = getattr(row, "key_label", None)
    raw = label if label is not None else getattr(row, _DIMENSION_KEY_ATTR[dimension], None)
    # Presentation maps on read: the facts table stores the RAW DB code
    # while the runs API emits dotted codes, so the error_code dimension
    # canonicalizes each key (legacy `task_failure` -> `harness.worker_failed`)
    # to match the runs UI — variants collapse into one chart slice.
    if dimension == AnalyticsDimension.ERROR_CODE and raw is not None:
        raw = map_legacy_code(str(raw))
    # Normalize to a comparable string: dimension keys may be UUID ids
    # (folder_id/pipeline_id/team_id fallback when the snapshot label is
    # NULL) or label strings. Never emit a raw UUID — mixing UUID and
    # None in the bucket key crashes `sorted` and breaks the
    # ``str | None`` response model (AnalyticsBucket.key).
    return str(raw) if raw is not None else None


def _accumulate_row(bucket: dict[str, Any], row: Any, cnt: int) -> None:
    bucket["count"] += cnt
    bucket["complete"] += int(getattr(row, "complete_count", None) or 0)
    bucket["failure"] += int(getattr(row, "failure_count", None) or 0)
    bucket["stall"] += int(getattr(row, "stall_count", None) or 0)
    if row.total_cost_usd is not None:
        bucket["cost"] = (bucket["cost"] or Decimal(0)) + Decimal(str(row.total_cost_usd))
    if row.total_tokens is not None:
        bucket["tokens"] = (bucket["tokens"] or 0) + int(row.total_tokens)
    if row.avg_duration_ms is not None:
        bucket["duration_sum"] += float(row.avg_duration_ms) * cnt
        bucket["duration_n"] += cnt
    avg_queue_wait = getattr(row, "avg_queue_wait_ms", None)
    if avg_queue_wait is not None:
        bucket["queue_wait_sum"] += float(avg_queue_wait) * cnt
        bucket["queue_wait_n"] += cnt
    avg_final_idle = getattr(row, "avg_final_idle_ms", None)
    if avg_final_idle is not None:
        bucket["final_idle_sum"] += float(avg_final_idle) * cnt
        bucket["final_idle_n"] += cnt
    avg_output = getattr(row, "avg_output_bytes", None)
    if avg_output is not None:
        bucket["output_bytes_sum"] += float(avg_output) * cnt
        bucket["output_bytes_n"] += cnt


def _build_time_grid(group_by: AnalyticsGroupBy, date_from: date, date_to: date) -> list[date] | list[datetime]:
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
    return grid_times


def _bucket_dim_keys(
    agg: dict[tuple[Any, Any | None], dict[str, Any]], dimension: AnalyticsDimension | None
) -> list[Any]:
    dim_keys: list[Any] = [None]
    if dimension is not None:
        # All keys are already normalized to ``str | None``, so the sort is
        # None-safe. An empty range (no observed dimension keys) falls back to
        # ``[None]`` so a dimensioned query still zero-fills the requested grid
        # — same shape as the non-dimensioned case.
        dim_keys = sorted({bk[1] for bk in agg}, key=lambda k: (k is None, k or "")) or [None]
    return dim_keys


def _bucket_averages(
    b: dict[str, Any] | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    avg_dur = (b["duration_sum"] / b["duration_n"]) if b and b["duration_n"] else None
    avg_queue_wait = (b["queue_wait_sum"] / b["queue_wait_n"]) if b and b["queue_wait_n"] else None
    avg_final_idle = (b["final_idle_sum"] / b["final_idle_n"]) if b and b["final_idle_n"] else None
    avg_output_bytes = (b["output_bytes_sum"] / b["output_bytes_n"]) if b and b["output_bytes_n"] else None
    return (avg_dur, avg_queue_wait, avg_final_idle, avg_output_bytes)


def _format_iso(tkey: date | datetime) -> str:
    return tkey.replace(tzinfo=None).isoformat() if isinstance(tkey, datetime) else tkey.isoformat()


def _emit_bucket_row(b: dict[str, Any] | None, tkey: date | datetime, dkey: Any | None) -> dict[str, Any]:
    count = b["count"] if b else 0
    complete = b["complete"] if b else 0
    cost = float(b["cost"]) if b and b["cost"] is not None else None
    tokens = b["tokens"] if b else None
    avg_dur, avg_queue_wait, avg_final_idle, avg_output_bytes = _bucket_averages(b)
    success_rate = (complete / count) if count else None
    return {
        "date": _format_iso(tkey),
        "key": dkey,
        "count": count,
        "total_cost_usd": cost,
        "total_tokens": tokens,
        "avg_duration_ms": round(avg_dur, 1) if avg_dur is not None else None,
        "success_rate": round(success_rate, 4) if success_rate is not None else None,
        "failure_count": b["failure"] if b else 0,
        "stall_count": b["stall"] if b else 0,
        "avg_queue_wait_ms": round(avg_queue_wait, 1) if avg_queue_wait is not None else None,
        "avg_final_idle_ms": round(avg_final_idle, 1) if avg_final_idle is not None else None,
        "avg_output_bytes": round(avg_output_bytes, 1) if avg_output_bytes is not None else None,
    }


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
        dkey = _row_dimension_key(row, dimension)
        bkey: tuple[Any, Any | None] = (tkey, dkey)
        bucket = agg.get(bkey)
        if bucket is None:
            bucket = _empty_bucket()
            agg[bkey] = bucket
        cnt = int(row.count or 0)
        _accumulate_row(bucket, row, cnt)

    # Explicit time grid: hourly (from date_from 00:00 UTC to date_to 23:59 UTC)
    # for hour grouping, otherwise the day grid (week Mondays for week grouping).
    grid_times = _build_time_grid(group_by, date_from, date_to)

    dim_keys = _bucket_dim_keys(agg, dimension)

    out: list[dict[str, Any]] = []
    for tkey in grid_times:
        for dkey in dim_keys:
            out_key: tuple[Any, Any | None] = (tkey, dkey)
            b = agg.get(out_key)
            out.append(_emit_bucket_row(b, tkey, dkey))

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
    AnalyticsDimension.ERROR_CODE: "error_code",
}
