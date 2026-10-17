"""Capacity / queue-pressure observability (FAR-1343).

Answers, from ONE read surface, the two operator questions the ticket names:

* **"Is my queue draining, and if not, which limit is holding it?"** — per
  pipeline and org-wide pending depth, oldest-pending age, created/started/
  completed counts (the drain rate), the cause marker each pending run carries,
  the configured limits, and the limit that currently binds.
* **"Is the org's ACTIVE-RUN resource healthy?"** — cap utilisation vs the
  completion rate, the *"cap fully held AND completions ≈ 0 for K consecutive
  intervals"* leak/stall signature, and attribution to the pipeline(s) holding
  the slots. This is the signal that would have caught the 2026-10-15 incident
  (a small pending queue while the org cap was fully held by non-completing
  ``running`` runs).

This is **observability only — signal, never a refusal.** Nothing here gates
admission; the refusal paths live in the dispatcher/executor capacity gates.

Metric-correctness notes (deliberate, and different from older readers):

* The active-run count excludes ``pending`` (it holds no slot) and reports the
  human-waiting statuses (``awaiting_human`` / ``hitl_parked``) SEPARATELY.
  ``executing`` = running/claimed/unknown — the set that actually holds a slot.
  ``gate_total`` = executing + parked, because the ORG admission gate
  deliberately counts parked runs (FAR-604 D1: the org-wide pool is bounded).
  Both are reported so an operator can see a cap held by parked rows rather than
  by executing ones.
* Pending depth excludes ``cancellation_requested`` rows, matching the
  canonical ``get_pipeline_queue_depth`` reader (the backpressure gate).
* A queued interval ends at ``started_at``, else ``completed_at`` (a run
  cancelled before it ever started stops counting), else *now* (still pending).
  This deliberately does NOT reproduce the daily-facts ``queued`` reader's
  "never started => queued forever" behaviour (a known FAR-1710 class issue);
  the live reader must not inflate depth with cancelled rows.
* Slots are attributed from the status the row holds at read time. An org cap
  held entirely by ``awaiting_human`` rows is visible as ``parked_total`` so it
  is not mistaken for a leak of executing runs. The leak/stall signal therefore
  requires the **executing** total to hold the cap (FAR-1343 M2): a cap held
  only by human-waiting rows is never reported as ``stall_suspected``.
* The active-run health section is computed from an **org-wide** row set, not
  the caller's team-scoped one (FAR-1343 M3). Its denominator
  (``org_run_concurrency_limit``) is org-wide and the panel labels it "Org cap
  use", so the numerator must be org-wide too — comparing a team's runs against
  the org's cap reported a false utilisation and a false ``cap_held``. The
  queue/drain/series sections stay team-scoped.

The scan is bounded (``CAPACITY_MAX_RAW_ROWS``); overflow is a typed validation
error telling the caller to narrow the window — never a silent truncation (a
partial scan yields wrong depth/age numbers). The bucket count is bounded too
(``MAX_CAPACITY_BUCKETS``), and the interval series is built by a single pass
over the rows rather than rescanning them per bucket (FAR-1343 M4).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.analytics.builder import to_utc_aware
from modulo.core.analytics.service import (
    _ERR_DATABASE_UNAVAILABLE,
    _ERR_RATE_LIMIT_EXCEEDED,
    AnalyticsDatabaseError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
    _guarded_session,
    _rate_limited,
)
from modulo.db.crud.run import (
    ERROR_CODE_ORG_CAPACITY_LIMITED,
    ERROR_CODE_PIPELINE_CAPACITY,
    get_org_run_concurrency_limit,
)
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import ACTIVE_RUN_STATUSES, AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS, Run
from modulo.settings import Settings

# Raw-row cap for the capacity scan. The window is short by design (minutes /
# hours), so a bigger scan means a pathological org or a bad window — reject it
# with an actionable message rather than bucket a truncated scan.
CAPACITY_MAX_RAW_ROWS = 50_000

# Upper bound on the number of intervals the series may contain. The report is
# synchronous inside an async route (no statement timeout), and the interval
# series is the only user-controlled dimension that multiplies the row scan —
# 1440 minutes at a 60s interval would be 1,440 buckets over up to
# ``CAPACITY_MAX_RAW_ROWS`` rows. Above this, the caller is asked to widen the
# interval or narrow the window (FAR-1343 M4). The default request is 12.
MAX_CAPACITY_BUCKETS = 480

EXECUTING_STATUSES: frozenset[str] = frozenset(
    ACTIVE_RUN_STATUSES - {"pending", AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS}
)
PARKED_STATUSES: frozenset[str] = frozenset({AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS})
GATE_STATUSES: frozenset[str] = frozenset(EXECUTING_STATUSES | PARKED_STATUSES)
LIVE_STATUSES: frozenset[str] = frozenset(ACTIVE_RUN_STATUSES)

# Cause buckets. The raw DB markers are ``pipeline_capacity`` /
# ``org_capacity_limited``; the executor's human detail distinguishes an org
# SANDBOX denial from an org RUN denial, both of which write the same raw
# marker, so the detail is parsed for the sub-cause.
CAUSE_PIPELINE = "pipeline"
CAUSE_ORG_RUN = "org_run"
CAUSE_SANDBOX = "sandbox"
CAUSE_UNMARKED = "unmarked"
_CAUSES: tuple[str, ...] = (CAUSE_PIPELINE, CAUSE_ORG_RUN, CAUSE_SANDBOX, CAUSE_UNMARKED)

_SANDBOX_DETAIL_PREFIX = "Org sandbox concurrency limit reached"
_ORG_RUN_DETAIL_PREFIX = "Org run concurrency limit reached"

_DEFAULT_STALL_INTERVALS = 3
_DEFAULT_WINDOW_MINUTES = 60
_DEFAULT_INTERVAL_SECONDS = 300
_MIN_WINDOW_MINUTES = 5
_MAX_WINDOW_MINUTES = 1440
_MIN_INTERVAL_SECONDS = 60
_MAX_INTERVAL_SECONDS = 3600
_MAX_STALL_INTERVALS = 12


@dataclass(frozen=True)
class CapacityParams:
    """Typed, validated knobs for the capacity read."""

    window_minutes: int = _DEFAULT_WINDOW_MINUTES
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS
    stall_intervals: int = _DEFAULT_STALL_INTERVALS
    pipeline_ids: tuple[uuid.UUID, ...] = ()
    team_ids: tuple[uuid.UUID, ...] | None = None


def validate_capacity_params(params: CapacityParams) -> CapacityParams:
    """Clamp/reject the window knobs (typed error, never a silent default)."""
    if not _MIN_WINDOW_MINUTES <= params.window_minutes <= _MAX_WINDOW_MINUTES:
        raise AnalyticsValidationError(
            f"window_minutes must be between {_MIN_WINDOW_MINUTES} and {_MAX_WINDOW_MINUTES}"
        )
    if not _MIN_INTERVAL_SECONDS <= params.interval_seconds <= _MAX_INTERVAL_SECONDS:
        raise AnalyticsValidationError(
            f"interval_seconds must be between {_MIN_INTERVAL_SECONDS} and {_MAX_INTERVAL_SECONDS}"
        )
    if not 1 <= params.stall_intervals <= _MAX_STALL_INTERVALS:
        raise AnalyticsValidationError(f"stall_intervals must be between 1 and {_MAX_STALL_INTERVALS}")
    if params.interval_seconds > params.window_minutes * 60:
        raise AnalyticsValidationError("interval_seconds must not exceed the window length")
    bucket_count = (params.window_minutes * 60) // params.interval_seconds
    if bucket_count > MAX_CAPACITY_BUCKETS:
        raise AnalyticsValidationError(
            f"window_minutes / interval_seconds yields {bucket_count} intervals; "
            f"the maximum is {MAX_CAPACITY_BUCKETS} — widen interval_seconds or narrow window_minutes"
        )
    return params


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def _team_scope(stmt: Any, params: CapacityParams) -> Any:
    """Apply the caller's team boundary (own teams + org-level pipelines).

    ``team_ids is None`` means unconstrained (an org admin). An empty tuple is a
    FAIL-CLOSED boundary: only org-level pipelines (``owner_team_id IS NULL``)
    are visible, matching the analytics facts/concurrency scope.
    """
    if params.team_ids is None:
        return stmt
    stmt = stmt.outerjoin(Pipeline, Pipeline.id == Run.pipeline_id)
    if not params.team_ids:
        return stmt.where(Pipeline.owner_team_id.is_(None))
    return stmt.where(sa.or_(Pipeline.owner_team_id.is_(None), Pipeline.owner_team_id.in_(params.team_ids)))


def build_capacity_rows_query(
    org_id: uuid.UUID,
    params: CapacityParams,
    *,
    window_start: datetime,
) -> Any:
    """Select the per-run lifecycle facts the report is computed from.

    Includes any run that overlaps the window in ANY dimension (created, started
    or completed inside it) PLUS every run currently in a live status — a queue
    entry older than the window must still appear in the current depth, and a
    long-running leaked slot must still appear in the active attribution.
    """
    stmt = sa.select(
        Run.pipeline_id,
        Run.status,
        Run.created_at,
        Run.started_at,
        Run.completed_at,
        Run.error_code,
        Run.error_detail,
        Run.heartbeat_at,
        Run.cancellation_requested,
    ).where(
        Run.organisation_id == org_id,
        sa.or_(
            Run.created_at >= window_start,
            Run.started_at >= window_start,
            Run.completed_at >= window_start,
            Run.status.in_(LIVE_STATUSES),
        ),
    )
    if params.pipeline_ids:
        stmt = stmt.where(Run.pipeline_id.in_(params.pipeline_ids))
    stmt = _team_scope(stmt, params)
    return stmt.order_by(Run.created_at).limit(CAPACITY_MAX_RAW_ROWS + 1)


def build_pipeline_meta_query(org_id: uuid.UUID, params: CapacityParams) -> Any:
    """The configured per-pipeline limits + labels for the same scope."""
    stmt = sa.select(
        Pipeline.id,
        Pipeline.name,
        Pipeline.max_concurrent_runs,
    ).where(Pipeline.organisation_id == org_id)
    if params.pipeline_ids:
        stmt = stmt.where(Pipeline.id.in_(params.pipeline_ids))
    if params.team_ids is not None:
        if not params.team_ids:
            stmt = stmt.where(Pipeline.owner_team_id.is_(None))
        else:
            stmt = stmt.where(sa.or_(Pipeline.owner_team_id.is_(None), Pipeline.owner_team_id.in_(params.team_ids)))
    return stmt.order_by(Pipeline.name)


def build_capacity_health_query(org_id: uuid.UUID, *, window_start: datetime) -> Any:
    """Org-wide active-run facts for the active-health section (FAR-1343 M3).

    Deliberately NOT team- or pipeline-scoped: the section's denominator
    (``org_run_concurrency_limit``) is org-wide and the panel labels it "Org cap
    use", so the numerator must be org-wide too. A team- or pipeline-scoped
    numerator compared against an org-wide cap reports a false utilisation, a
    false ``cap_held`` and (via M2) a silently unavailable stall signal.

    Includes every currently-live status (a long-running leaked slot older than
    the window must still be attributed) PLUS any run completed inside the
    window (the trailing zero-completion signal the stall detector reads).
    """
    return (
        sa.select(
            Run.pipeline_id,
            Run.status,
            Run.created_at,
            Run.started_at,
            Run.completed_at,
            Run.heartbeat_at,
            Run.cancellation_requested,
        )
        .where(
            Run.organisation_id == org_id,
            sa.or_(
                Run.completed_at >= window_start,
                Run.status.in_(LIVE_STATUSES),
            ),
        )
        .limit(CAPACITY_MAX_RAW_ROWS + 1)
    )


# ---------------------------------------------------------------------------
# Pure computation
# ---------------------------------------------------------------------------


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    return to_utc_aware(value) if isinstance(value, datetime) else None


def _seconds(delta: timedelta) -> float:
    return round(max(0.0, delta.total_seconds()), 1)


def _cause_of(row: Any) -> str:
    """Classify a pending row's capacity-block cause.

    ``pipeline_capacity`` is unambiguous. ``org_capacity_limited`` covers both
    the org-run and the org-sandbox gates (the executor writes the same raw
    marker for both), so the human detail picks the sub-cause. A pending row
    with no capacity marker was never denied at dispatch (it is simply waiting
    its turn) and is reported as ``unmarked``.
    """
    code = _field(row, "error_code")
    if code == ERROR_CODE_PIPELINE_CAPACITY:
        return CAUSE_PIPELINE
    if code != ERROR_CODE_ORG_CAPACITY_LIMITED:
        return CAUSE_UNMARKED
    detail = _field(row, "error_detail") or ""
    if detail.startswith(_SANDBOX_DETAIL_PREFIX):
        return CAUSE_SANDBOX
    if detail.startswith(_ORG_RUN_DETAIL_PREFIX):
        return CAUSE_ORG_RUN
    # An org marker with no recognised detail: report it under the org-run
    # bucket so an org-level block is never counted as "unmarked" (which would
    # imply no limit was involved).
    return CAUSE_ORG_RUN


def _bucket_boundaries(window_start: datetime, now: datetime, interval_seconds: int) -> list[datetime]:
    """Interval boundaries covering ``[window_start, now]`` (last edge == now)."""
    step = timedelta(seconds=interval_seconds)
    bounds = [window_start]
    cursor = window_start + step
    while cursor < now:
        bounds.append(cursor)
        cursor += step
    bounds.append(now)
    return bounds


def _queued_intervals(
    rows: list[Any],
    *,
    now: datetime,
) -> dict[uuid.UUID, list[tuple[datetime, datetime | None]]]:
    """Per-pipeline half-open ``[created, queued_end)`` queued intervals.

    Excludes cancellation-requested rows (the canonical pending reader does).
    ``queued_end`` = started_at, else completed_at (a run that never started and
    is already terminal stops being queued when it terminated), else ``None``
    meaning OPEN — the run is still queued, so it covers every instant up to and
    including now.
    """
    intervals: dict[uuid.UUID, list[tuple[datetime, datetime | None]]] = defaultdict(list)
    for row in rows:
        if _field(row, "cancellation_requested"):
            continue
        created = _aware(_field(row, "created_at"))
        if created is None or created > now:
            continue
        end = _aware(_field(row, "started_at")) or _aware(_field(row, "completed_at"))
        if end is not None and end <= created:
            continue
        intervals[_field(row, "pipeline_id")].append((created, min(end, now) if end is not None else None))
    return intervals


def _depth_series(
    intervals: list[tuple[datetime, datetime | None]],
    ats: list[datetime],
) -> list[tuple[int, float | None]]:
    """``(depth, oldest_age_seconds)`` at every instant in *ats* (ascending).

    One forward sweep instead of rescanning ``intervals`` per instant (FAR-1343
    M4: the build is synchronous in an async route, so the per-bucket rescan was
    O(buckets x rows)). Starts and ends are walked with two forward pointers and
    the oldest active start is tracked with a single monotonic index, so the
    whole series is ``O(S log S)`` to sort plus ``O(S + A)`` for *S* intervals
    and *A* instants.

    An interval covers *at* when ``start <= at`` and (``end is None`` or
    ``at < end``); an open interval covers every instant at or after its start.
    Note the comparison is always against a plain ``datetime`` — the ``end`` is
    never part of an ordering key, so an open (``None``) end alongside a closed
    one at the same start can never raise (FAR-1343 C1).
    """
    if not ats:
        return []
    by_start = sorted(intervals, key=lambda interval: interval[0])
    starts = [interval[0] for interval in by_start]
    ends = sorted(end for _, end in by_start if end is not None)
    results: list[tuple[int, float | None]] = []
    next_start = 0  # next interval start to activate
    next_end = 0  # next interval end to retire
    oldest = 0  # index into by_start of the oldest still-active interval
    depth = 0
    for at in ats:
        while next_start < len(starts) and starts[next_start] <= at:
            next_start += 1
            depth += 1
        while next_end < len(ends) and ends[next_end] <= at:
            next_end += 1
            depth -= 1
        # Advance the oldest candidate past intervals that have already ended.
        # ``oldest`` only ever moves forward, so this is amortised O(S).
        while oldest < next_start:
            end = by_start[oldest][1]
            if end is not None and end <= at:
                oldest += 1
                continue
            break
        if oldest < next_start:
            results.append((depth, _seconds(at - starts[oldest])))
        else:
            results.append((depth, None))
    return results


def _depth_at(intervals: list[tuple[datetime, datetime | None]], at: datetime) -> tuple[int, float | None]:
    """``(depth, oldest_age_seconds)`` of queued items at instant *at*."""
    return _depth_series(intervals, [at])[0]


def _count_in_window(row: Any, field: str, start: datetime, end: datetime) -> bool:
    value = _aware(_field(row, field))
    return value is not None and start <= value < end


def _pending_rows(rows: list[Any]) -> list[Any]:
    return [r for r in rows if _field(r, "status") == "pending" and not _field(r, "cancellation_requested")]


def _heartbeat_reference(row: Any, now: datetime) -> datetime | None:
    """The best "last seen alive" instant for a running row.

    ``heartbeat_at`` is the executor's liveness beat; fall back to
    ``started_at`` (a just-started row may not have beaten yet) and then
    ``created_at``.
    """
    if row is None:
        return None
    reference = _aware(_field(row, "heartbeat_at"))
    if reference is not None:
        return reference
    started = _aware(_field(row, "started_at"))
    if started is not None and started <= now:
        return started
    return _aware(_field(row, "created_at"))


def _active_health_counts(
    rows: list[Any],
    *,
    now: datetime,
    stale_heartbeat_seconds: int,
    pipeline_name: dict[str, Any],
) -> dict[str, Any]:
    """Count the gate/executing/parked rows and build per-pipeline attribution.

    Extracted so the org-wide health row set can be counted independently of
    the (team-scoped) queue row set — see ``build_capacity_health_query``.
    """
    executing_total = 0
    running_total = 0
    parked_total = 0
    gate_total = 0
    cancelling_total = 0
    attribution: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "pipeline_id": None,
            "pipeline_name": None,
            "executing": 0,
            "running": 0,
            "parked": 0,
            "oldest_executing_age_seconds": None,
            "stale_heartbeat_count": 0,
        }
    )
    for row in rows:
        status = _field(row, "status")
        if status not in GATE_STATUSES:
            continue
        pid = str(_field(row, "pipeline_id"))
        att = attribution[pid]
        att["pipeline_id"] = pid
        att["pipeline_name"] = pipeline_name.get(pid)
        if _field(row, "cancellation_requested"):
            # The admission gate excludes cancellation-requested rows; count
            # them for transparency but never as held capacity.
            cancelling_total += 1
            continue
        gate_total += 1
        if status in PARKED_STATUSES:
            parked_total += 1
            att["parked"] += 1
            continue
        executing_total += 1
        att["executing"] += 1
        if status == "running":
            running_total += 1
            att["running"] += 1
            running_started_at = _aware(_field(row, "started_at"))
            if running_started_at is not None:
                age = _seconds(now - running_started_at)
                current = att["oldest_executing_age_seconds"]
                att["oldest_executing_age_seconds"] = age if current is None else max(current, age)
            reference = _heartbeat_reference(row, now)
            if reference is None or (now - reference).total_seconds() > stale_heartbeat_seconds:
                att["stale_heartbeat_count"] += 1

    attribution_list = sorted(attribution.values(), key=lambda a: (-a["executing"], a["pipeline_name"] or ""))
    return {
        "gate_total": gate_total,
        "executing_total": executing_total,
        "running_total": running_total,
        "parked_total": parked_total,
        "cancelling_total": cancelling_total,
        "attribution": attribution_list,
        "stale_heartbeat_count": sum(a["stale_heartbeat_count"] for a in attribution_list),
    }


def _trailing_zero_completion_intervals(
    rows: list[Any],
    bounds: list[datetime],
    *,
    now: datetime,
    window_start: datetime,
    interval_seconds: int,
) -> int:
    """Trailing run of COMPLETE intervals with zero completions.

    Counted from the org-wide health rows so the stall signal's completion side
    matches its org-wide cap denominator (FAR-1343 M3). Completions are binned
    in a single pass (FAR-1343 M4) rather than rescanned per bucket. The partial
    current interval is never counted (it must not read as "nothing completed
    while at cap").
    """
    counts = _bin_counts(rows, "completed_at", bounds, window_start=window_start, interval_seconds=interval_seconds)
    zero_completion_intervals = 0
    for index in range(len(counts) - 1, -1, -1):
        start, end = bounds[index], bounds[index + 1]
        if not (end < now and (end - start).total_seconds() >= interval_seconds):
            continue
        if counts[index] == 0:
            zero_completion_intervals += 1
        else:
            break
    return zero_completion_intervals


def _bin_counts(
    rows: list[Any],
    field: str,
    bounds: list[datetime],
    *,
    window_start: datetime,
    interval_seconds: int,
) -> list[int]:
    """One pass over *rows* into per-bucket counts for *field* (FAR-1343 M4).

    Replaces the per-bucket ``sum(1 for r in rows ...)`` rescan (O(buckets x
    rows)) with a single pass (O(rows)), mapping each value to its bucket by
    index. Semantics match ``_count_in_window``: a value belongs to bucket *i*
    when ``bounds[i] <= value < bounds[i + 1]``.
    """
    counts = [0] * (len(bounds) - 1)
    bucket_total = len(counts)
    for row in rows:
        value = _aware(_field(row, field))
        if value is None or value < window_start or value >= bounds[-1]:
            continue
        index = int((value - window_start).total_seconds() // interval_seconds)
        if index >= bucket_total:
            index = bucket_total - 1
        counts[index] += 1
    return counts


def compute_capacity_report(
    *,
    rows: list[Any],
    pipelines: list[Any],
    org_run_limit: int | None,
    sandbox_cap: int | None,
    sandbox_is_default: bool,
    sandbox_active: int,
    now: datetime,
    window_start: datetime,
    interval_seconds: int,
    stall_intervals: int,
    stale_heartbeat_seconds: int,
    health_rows: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the whole capacity report from already-fetched rows (pure).

    *rows* drive the team-scoped queue/drain/series sections. *health_rows*
    (defaulting to *rows*) drive the org-wide active-run health section — the
    caller passes a genuinely org-wide row set when the queue rows are
    team-scoped, so the org cap ratio and the leak/stall signal are computed
    against the org-wide cap they are labelled with (FAR-1343 M3).
    """
    now = to_utc_aware(now)
    window_start = to_utc_aware(window_start)
    active_rows = rows if health_rows is None else health_rows
    pipeline_name = {str(_field(p, "id")): _field(p, "name") for p in pipelines}
    pipeline_cap = {str(_field(p, "id")): int(_field(p, "max_concurrent_runs") or 0) for p in pipelines}

    pending = _pending_rows(rows)
    cause_counts = Counter(dict.fromkeys(_CAUSES, 0))
    per_pipeline_pending: dict[str, list[Any]] = defaultdict(list)
    for row in pending:
        cause_counts[_cause_of(row)] += 1
        per_pipeline_pending[str(_field(row, "pipeline_id"))].append(row)

    pending_created = [c for c in (_aware(_field(r, "created_at")) for r in pending) if c is not None]
    oldest_pending: datetime | None = min(pending_created) if pending_created else None
    pending_by_pipeline: list[dict[str, Any]] = []
    for pid, items in per_pipeline_pending.items():
        item_created = [c for c in (_aware(_field(r, "created_at")) for r in items) if c is not None]
        oldest = min(item_created) if item_created else None
        pending_by_pipeline.append(
            {
                "pipeline_id": pid,
                "pipeline_name": pipeline_name.get(pid),
                "pending_count": len(items),
                "oldest_pending_age_seconds": _seconds(now - oldest) if oldest else None,
                "max_concurrent_runs": pipeline_cap.get(pid),
            }
        )
    pending_by_pipeline.sort(key=lambda item: (-item["pending_count"], item["pipeline_name"] or ""))

    # --- active-run health (ORG-WIDE — see build_capacity_health_query) ----
    health = _active_health_counts(
        active_rows, now=now, stale_heartbeat_seconds=stale_heartbeat_seconds, pipeline_name=pipeline_name
    )
    gate_total = health["gate_total"]
    executing_total = health["executing_total"]
    running_total = health["running_total"]
    parked_total = health["parked_total"]
    cancelling_total = health["cancelling_total"]
    attribution_list = health["attribution"]
    attribution = {a["pipeline_id"]: a for a in attribution_list}

    # --- interval series + drain rate (team-scoped) ------------------------
    bounds = _bucket_boundaries(window_start, now, interval_seconds)
    queued = _queued_intervals(rows, now=now)
    all_queued = [interval for intervals in queued.values() for interval in intervals]
    baseline_depth, baseline_oldest_age = _depth_at(all_queued, window_start)

    bucket_ends = bounds[1:]
    overall_depths = _depth_series(all_queued, bucket_ends)
    pipeline_depths = {pipe_id: _depth_series(intervals, bucket_ends) for pipe_id, intervals in queued.items()}
    created_counts = _bin_counts(
        rows, "created_at", bounds, window_start=window_start, interval_seconds=interval_seconds
    )
    started_counts = _bin_counts(
        rows, "started_at", bounds, window_start=window_start, interval_seconds=interval_seconds
    )
    completed_counts = _bin_counts(
        rows, "completed_at", bounds, window_start=window_start, interval_seconds=interval_seconds
    )

    buckets: list[dict[str, Any]] = []
    for index in range(len(bounds) - 1):
        start, end = bounds[index], bounds[index + 1]
        created = created_counts[index]
        started = started_counts[index]
        completed = completed_counts[index]
        depth, oldest_age = overall_depths[index]
        by_pipeline: list[dict[str, Any]] = []
        for pipe_id, series in pipeline_depths.items():
            pipe_depth, pipe_oldest = series[index]
            if pipe_depth == 0 and pipe_oldest is None:
                continue
            by_pipeline.append(
                {
                    "pipeline_id": str(pipe_id),
                    "pipeline_name": pipeline_name.get(str(pipe_id)),
                    "pending_depth": pipe_depth,
                    "oldest_pending_age_seconds": pipe_oldest,
                }
            )
        by_pipeline.sort(key=lambda item: (-item["pending_depth"], item["pipeline_name"] or ""))
        # A bucket is COMPLETE only when it ends at or before now and spans a
        # full interval (the final bucket is the partial "so far" slice).
        complete = end < now and (end - start).total_seconds() >= interval_seconds
        buckets.append(
            {
                "interval_start": start.isoformat(),
                "interval_end": end.isoformat(),
                "created": created,
                "started": started,
                "completed": completed,
                "pending_depth": depth,
                "oldest_pending_age_seconds": oldest_age,
                "drain_ratio": round(started / created, 3) if created else None,
                "complete": complete,
                "by_pipeline": by_pipeline,
            }
        )

    created_total = sum(b["created"] for b in buckets)
    started_total = sum(b["started"] for b in buckets)
    completed_total = sum(b["completed"] for b in buckets)
    final_depth = buckets[-1]["pending_depth"] if buckets else len(pending)
    depth_change = final_depth - baseline_depth
    drain_ratio = round(started_total / created_total, 3) if created_total else None
    if not pending:
        verdict = "draining"
    elif depth_change > 0 or (drain_ratio is not None and drain_ratio < 1):
        verdict = "backing_up"
    elif depth_change < 0 or (drain_ratio is not None and drain_ratio >= 1):
        verdict = "draining"
    else:
        verdict = "stable"

    # Trailing zero-completion run over COMPLETE intervals only — the partial
    # current interval must never read as "nothing completed while at cap".
    # Counted from the ORG-WIDE health rows so the stall signal's completion
    # side matches its org-wide cap denominator.
    zero_completion_intervals = _trailing_zero_completion_intervals(
        active_rows, bounds, now=now, window_start=window_start, interval_seconds=interval_seconds
    )
    completions_in_window = sum(1 for row in active_rows if _count_in_window(row, "completed_at", window_start, now))

    cap_held = org_run_limit is not None and gate_total >= org_run_limit
    # FAR-1343 M2: the leak/stall signature is about EXECUTING runs holding the
    # cap, not runs parked on a human decision. ``gate_total`` deliberately
    # counts parked runs (they hold an org admission slot), so requiring only
    # ``cap_held`` would report a cap held entirely by human-waiting rows as a
    # leak. Require the executing total itself to reach the cap.
    executing_holds_cap = org_run_limit is not None and executing_total >= org_run_limit
    stall_suspected = bool(executing_holds_cap and zero_completion_intervals >= stall_intervals)
    if stall_suspected:
        verdict = "stalled"

    utilisation = round(gate_total / org_run_limit, 3) if org_run_limit else None

    # --- binding limit + headroom -----------------------------------------
    pipeline_capacity_pending = sum(
        1
        for row in pending
        if _cause_of(row) == CAUSE_PIPELINE
        and pipeline_cap.get(str(_field(row, "pipeline_id"))) is not None
        and attribution.get(str(_field(row, "pipeline_id")), {}).get("executing", 0)
        >= pipeline_cap.get(str(_field(row, "pipeline_id")), 0)
    )
    org_marked_pending = sum(1 for row in pending if _cause_of(row) in (CAUSE_ORG_RUN, CAUSE_SANDBOX))

    binding: dict[str, Any] = {
        "scope": "none",
        "limit": None,
        "in_use": None,
        "headroom": None,
        "pending_blocked": 0,
        "admissible_if_raised": 0,
    }
    if sandbox_cap is not None and sandbox_active >= sandbox_cap:
        binding = {
            "scope": "sandbox",
            "limit": sandbox_cap,
            "in_use": sandbox_active,
            "headroom": 0,
            "pending_blocked": org_marked_pending,
            "admissible_if_raised": min(len(pending), org_marked_pending or len(pending)),
        }
    elif org_run_limit is not None and gate_total >= org_run_limit:
        binding = {
            "scope": "org_run",
            "limit": org_run_limit,
            "in_use": gate_total,
            "headroom": 0,
            "pending_blocked": org_marked_pending,
            "admissible_if_raised": min(len(pending), org_marked_pending or len(pending)),
        }
    else:
        bound_pipeline = next(
            (
                item
                for item in pending_by_pipeline
                if item["max_concurrent_runs"] is not None
                and attribution.get(str(item["pipeline_id"]), {}).get("executing", 0) >= item["max_concurrent_runs"]
            ),
            None,
        )
        if bound_pipeline is not None:
            cap = bound_pipeline["max_concurrent_runs"] or 0
            in_use = attribution.get(str(bound_pipeline["pipeline_id"]), {}).get("executing", 0)
            binding = {
                "scope": "pipeline",
                "limit": cap,
                "in_use": in_use,
                "headroom": max(0, cap - in_use),
                "pending_blocked": pipeline_capacity_pending,
                "admissible_if_raised": min(
                    bound_pipeline["pending_count"],
                    pipeline_capacity_pending or bound_pipeline["pending_count"],
                ),
            }

    pending_after_raise = max(0, len(pending) - binding["admissible_if_raised"])

    return {
        "generated_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_seconds": int((now - window_start).total_seconds()),
        "interval_seconds": interval_seconds,
        "limits": {
            "org_run_concurrency_limit": org_run_limit,
            "sandbox_concurrency_limit": sandbox_cap,
            "sandbox_limit_is_default": sandbox_is_default,
        },
        "pending": {
            "total": len(pending),
            "oldest_pending_age_seconds": _seconds(now - oldest_pending) if oldest_pending else None,
            "by_cause": {cause: cause_counts[cause] for cause in _CAUSES},
            "by_pipeline": pending_by_pipeline,
        },
        "drain": {
            "created": created_total,
            "started": started_total,
            "completed": completed_total,
            "ratio": drain_ratio,
            "baseline_depth": baseline_depth,
            "baseline_oldest_pending_age_seconds": baseline_oldest_age,
            "final_depth": final_depth,
            "depth_change": depth_change,
            "verdict": verdict,
        },
        "binding": binding,
        "queue_if_raised": {
            "pending_now": len(pending),
            "admissible_if_binding_raised": binding["admissible_if_raised"],
            "pending_after_raise": pending_after_raise,
        },
        "active_health": {
            "org_run_limit": org_run_limit,
            "gate_total": gate_total,
            "executing_total": executing_total,
            "running_total": running_total,
            "parked_total": parked_total,
            "cancelling_total": cancelling_total,
            "utilisation": utilisation,
            "cap_held": cap_held,
            "completions_in_window": completions_in_window,
            "zero_completion_intervals": zero_completion_intervals,
            "stall_intervals": stall_intervals,
            "stall_suspected": stall_suspected,
            "stale_heartbeat_threshold_seconds": stale_heartbeat_seconds,
            "stale_heartbeat_count": health["stale_heartbeat_count"],
            "attribution": attribution_list,
        },
        "series": buckets,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


async def run_capacity_query(
    *,
    org_id: uuid.UUID,
    params: CapacityParams,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    account_id: uuid.UUID | None = None,
    org_role: str | None = None,
) -> dict[str, Any]:
    """Read the live queue + active-run health report (FAR-1343).

    One bounded session: RLS org context, statement timeout, the run scan, the
    configured limits, and the sandbox gate's cap/count. The sandbox count is
    the SAME reader the dispatch gate uses, so the utilisation shown here is the
    utilisation the gate enforces.

    Two row scans are issued: the team-scoped queue scan (``params``) and an
    org-wide active-health scan (``build_capacity_health_query``). The health
    section's denominator is the org-wide ``org_run_concurrency_limit``, so its
    numerator must be org-wide too (FAR-1343 M3) — a team viewer otherwise saw
    their team's runs measured against the whole org's cap.
    """
    params = validate_capacity_params(params)
    if _rate_limited(str(org_id)):
        raise AnalyticsRateLimitedError(_ERR_RATE_LIMIT_EXCEEDED)

    now = datetime.now(UTC)
    window_start = now - timedelta(minutes=params.window_minutes)
    stale_heartbeat_seconds = getattr(settings, "slot_reconcile_stale_seconds", 1800)

    try:
        async with _guarded_session(
            factory, settings, org_id=org_id, account_id=account_id, org_role=org_role
        ) as session:
            rows = list(
                (await session.execute(build_capacity_rows_query(org_id, params, window_start=window_start))).all()
            )
            if len(rows) > CAPACITY_MAX_RAW_ROWS:
                raise AnalyticsValidationError(
                    f"capacity scan exceeded the {CAPACITY_MAX_RAW_ROWS}-row cap — "
                    "narrow the window or add a pipeline filter"
                )
            health_rows = list(
                (await session.execute(build_capacity_health_query(org_id, window_start=window_start))).all()
            )
            if len(health_rows) > CAPACITY_MAX_RAW_ROWS:
                raise AnalyticsValidationError(
                    f"capacity active-run scan exceeded the {CAPACITY_MAX_RAW_ROWS}-row cap — narrow the window"
                )
            pipelines = list((await session.execute(build_pipeline_meta_query(org_id, params))).all())
            org_run_limit = await get_org_run_concurrency_limit(session, org_id)
            sandbox_cap, sandbox_is_default, sandbox_active = await _sandbox_state(session, org_id)
    except AnalyticsValidationError:
        raise
    except AnalyticsDatabaseError:
        raise
    except Exception as exc:  # pragma: no cover - defensive; _guarded_session maps DB errors
        raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from exc

    return compute_capacity_report(
        rows=rows,
        pipelines=pipelines,
        org_run_limit=org_run_limit,
        sandbox_cap=sandbox_cap,
        sandbox_is_default=sandbox_is_default,
        sandbox_active=sandbox_active,
        now=now,
        window_start=window_start,
        interval_seconds=params.interval_seconds,
        stall_intervals=params.stall_intervals,
        stale_heartbeat_seconds=int(stale_heartbeat_seconds),
        health_rows=health_rows,
    )


async def _sandbox_state(session: AsyncSession, org_id: uuid.UUID) -> tuple[int | None, bool, int]:
    """The sandbox cap the dispatch gate enforces, whether it is the default,
    and the gate's own current slot count.

    Delegates to the single readers (``read_runner_cap_contract`` +
    ``count_active_runner_dispatches_for_decision``) so the number shown is the
    number the gate applies — including the absent-key vs explicit ``null``
    distinction (absent = Docker-tier default only while the rollout flag is on;
    explicit null = no gate).
    """
    from modulo.core.runner_capacity import (
        count_active_runner_dispatches_for_decision,
        read_runner_cap_contract,
    )
    from modulo.db.crud.run import get_sandbox_concurrency_limit

    cap, host_resource_only = await read_runner_cap_contract(session, org_id)
    is_default = (await get_sandbox_concurrency_limit(session, org_id)).is_default
    if cap is None:
        return None, is_default, 0
    active = await count_active_runner_dispatches_for_decision(
        session,
        org_id,
        host_resource_only=host_resource_only,
    )
    return cap, is_default, active
