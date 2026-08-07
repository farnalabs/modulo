"""CRUD for Run records.

All functions require RLS org context to be set by the caller.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from operator import attrgetter
from typing import Any

from sqlalchemy import Date, case, cast, delete, func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from modulo.db.crud.base import PageResult
from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

# Capacity-block reason markers (B5). Set on error_code when a run is demoted
# back to pending because a capacity limit was hit; distinct from terminal
# failure codes (never_dispatched, worker_lost, capacity_timeout, ...).
ERROR_CODE_ORG_CAPACITY_LIMITED = "org_capacity_limited"
ERROR_CODE_PIPELINE_CAPACITY = "pipeline_capacity"
ERROR_CODE_CAPACITY_TIMEOUT = "capacity_timeout"
# Non-terminal markers that operators must be able to distinguish from real
# failures. The stale-run sweep exempts runs carrying these markers.
CAPACITY_MARKERS = frozenset({ERROR_CODE_ORG_CAPACITY_LIMITED, ERROR_CODE_PIPELINE_CAPACITY})

# Trigger types exempt from the org-wide pause. A new trigger type defaults to
# PAUSED (fail-closed) unless explicitly added here AND it passes trigger_id to
# create_run (types that create runs without a Trigger row, like scheduled
# reports / variants, are NOT pause-gated — see the create_run gate comment).
PAUSE_EXEMPT_TRIGGER_TYPES = frozenset({"manual", "correction"})

_SANDBOX_CONCURRENCY_KEY = "sandbox_concurrency_limit"
_SANDBOX_CONCURRENCY_MIN = 1
_SANDBOX_CONCURRENCY_MAX = 100

_RUN_CONCURRENCY_KEY = "run_concurrency_limit"
_RUN_CONCURRENCY_MIN = 1
_RUN_CONCURRENCY_MAX = 100


def _input_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 hex digest of a JSON-serialisable payload."""
    serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()


async def create_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    trigger_type: str,
    input_payload: dict[str, Any],
    account_id: uuid.UUID | None = None,
    trigger_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    rate_limit_key: str | None = None,
) -> Run:
    # Org-wide pause kill-switch — the SINGLE authority gate for trigger-initiated
    # runs (webhook, replay, cron, polling, agent_signal). Manual runs (POST /runs,
    # MCP trigger_pipeline), test_trigger (trigger_type="manual"), feedback
    # correction, and variant runs pass (trigger_id None or an exempt type).
    # A NEW trigger type defaults to PAUSED (fail-closed) unless explicitly added
    # to PAUSE_EXEMPT_TRIGGER_TYPES AND it passes trigger_id — a type that creates
    # a run WITHOUT a Trigger row (scheduled reports / variants) bypasses the gate
    # entirely (trigger_id=None) and is intentionally NOT pause-gated.
    #
    # Accepted bounded TOCTOU: a run whose gate read ``not paused`` and whose
    # INSERT commits after the toggle UPDATE lands is an "in-flight before pause"
    # run — benign, matches GitHub disable-workflow semantics; the pause takes
    # effect at the next statement boundary. Deliberately NO row locks (reviewed
    # decision). Read failures from ensure_triggers_resumable PROPAGATE — a DB
    # error is never fabricated into "paused". ``create_run`` calls
    # ``ensure_triggers_resumable`` (modulo.db.settings_resolver), which raises
    # ``TriggersPausedError`` (modulo.core.exceptions); that db->core edge is
    # exempted under the ``db-does-not-import-core`` contract in ``.importlinter``.
    if trigger_id is not None and trigger_type not in PAUSE_EXEMPT_TRIGGER_TYPES:
        from modulo.db.settings_resolver import ensure_triggers_resumable

        await ensure_triggers_resumable(session, org_id, trigger_id=trigger_id, trigger_type=trigger_type)

    run_id = uuid.uuid4()
    thread_id = f"{org_id}:{run_id}"
    result = await session.execute(
        text("SELECT COALESCE(MAX(run_number), 0) + 1 FROM runs WHERE organisation_id = :org_id"),
        {"org_id": org_id},
    )
    run_number = int(result.scalar_one() or 1)

    run = Run(
        id=run_id,
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type=trigger_type,
        input_hash=_input_hash(input_payload),
        input_payload=input_payload,
        account_id=account_id,
        trigger_id=trigger_id,
        owner_team_id=owner_team_id,
        langgraph_thread_id=thread_id,
        parent_run_id=parent_run_id,
        run_number=run_number,
        rate_limit_key=rate_limit_key,
    )
    session.add(run)
    await session.flush()
    return run


async def update_run_outputs(
    session: AsyncSession,
    run_id: uuid.UUID,
    outputs: dict[str, Any],
) -> Run | None:
    """Store per-node outputs for a completed run."""
    result = await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if run is None:
        return None
    run.outputs_json = outputs
    await session.flush()
    return run


async def get_run_io(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return the input_payload and outputs_json for a run."""
    run = await get_run(session, run_id)
    if run is None:
        return None
    return {
        "run_id": run_id,
        "run_number": run.run_number,
        "status": run.status,
        "input_payload": run.input_payload,
        "outputs_json": run.outputs_json,
    }


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    result = await session.execute(select(Run).where(Run.id == run_id))
    return result.scalar_one_or_none()


async def list_runs(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID | None = None,
    status: str | None = None,
    trigger_type: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
) -> PageResult[Run]:
    q = (
        select(Run)
        .options(selectinload(Run.pipeline))
        .join(Pipeline, Run.pipeline_id == Pipeline.id, isouter=False)
        .where(Pipeline.deleted_at.is_(None))
    )
    count_q = (
        select(func.count())
        .select_from(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id, isouter=False)
        .where(Pipeline.deleted_at.is_(None))
    )
    if pipeline_id is not None:
        q = q.where(Run.pipeline_id == pipeline_id)
        count_q = count_q.where(Run.pipeline_id == pipeline_id)
    if status is not None:
        q = q.where(Run.status == status)
        count_q = count_q.where(Run.status == status)
    if trigger_type is not None:
        q = q.where(Run.trigger_type == trigger_type)
        count_q = count_q.where(Run.trigger_type == trigger_type)
    if search is not None:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.where(Pipeline.name.ilike(f"%{escaped}%", escape="\\"))
        count_q = count_q.where(Pipeline.name.ilike(f"%{escaped}%", escape="\\"))

    if cursor is not None:
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            q,
            cursor=cursor,
            limit=page_size,
            model=Run,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=page,
            page_size=page_size,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    offset = (page - 1) * page_size
    try:
        total = (await session.execute(count_q)).scalar_one_or_none() or 0
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    items = list((await session.execute(q.order_by(Run.created_at.desc()).offset(offset).limit(page_size))).scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


_COST_ROLLUP_QUANTUM = Decimal("0.000001")


async def get_child_runs_cost(
    session: AsyncSession,
    parent_run_ids: list[uuid.UUID],
) -> dict[uuid.UUID, Decimal]:
    """Sum child-run ``total_cost_usd`` per parent run (cost rollup).

    Returns ``{parent_run_id: total}`` from a single ``GROUP BY`` query so
    callers avoid N+1 aggregation over the runs list. Parents with no children
    -- or only NULL-cost children -- are absent from the dict; callers treat a
    missing key as zero. NULL ``total_cost_usd`` children contribute 0 to the
    SUM. Values are quantized to 6 decimal places to match the
    ``Numeric(14, 6)`` column scale (an all-NULL group sums to ``0.000000``).
    """
    if not parent_run_ids:
        return {}
    result = await session.execute(
        select(Run.parent_run_id, func.coalesce(func.sum(Run.total_cost_usd), 0))
        .where(Run.parent_run_id.in_(parent_run_ids))
        .group_by(Run.parent_run_id)
    )
    rollup: dict[uuid.UUID, Decimal] = {}
    for parent_id, cost in result.all():
        rollup[uuid.UUID(str(parent_id))] = Decimal(str(cost)).quantize(_COST_ROLLUP_QUANTUM)
    return rollup


_COST_BREAKDOWN_SENTINEL: Any = object()


async def update_run_status(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: str,
    *,
    error_code: str | None = None,
    error_detail: str | None = None,
    total_tokens: int | None = None,
    total_cost_usd: Decimal | None = None,
    cost_breakdown: Any = _COST_BREAKDOWN_SENTINEL,
    node_token_usage: dict[str, Any] | None = None,
    outputs_json: dict[str, Any] | None = None,
    claimed_by: str | None = None,
    clear_error_code: bool = False,
) -> Run | None:
    result = await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if run is None:
        return None
    run.status = status
    if status == "running" and run.started_at is None:
        run.started_at = datetime.now(UTC)
    if claimed_by is not None:
        run.claimed_by = claimed_by
    if status in ("complete", "failed", "cancelled", "eval_failed"):
        run.completed_at = datetime.now(UTC)
    if clear_error_code:
        # Explicitly clear a prior capacity marker (the error_code=... writes
        # below are conditional on non-None, so None alone cannot clear it).
        run.error_code = None
        run.error_detail = None
    if error_code is not None:
        run.error_code = error_code
    if error_detail is not None:
        run.error_detail = error_detail
    if total_tokens is not None:
        run.total_tokens = total_tokens
    if total_cost_usd is not None:
        run.total_cost_usd = total_cost_usd
    if cost_breakdown is not _COST_BREAKDOWN_SENTINEL:
        # The eval_failed direct write PRESERVES the terminal field set: it
        # sets status + completed_at and leaves the cost fields untouched (the
        # eval pipeline never passes the cost kwargs). Passing the sentinel
        # (the default) means "leave cost_breakdown alone"; passing None writes
        # an explicit NULL (the pre-component-read terminal transition).
        run.cost_breakdown = cost_breakdown
    if node_token_usage is not None:
        run.node_token_usage = node_token_usage
    if outputs_json is not None:
        run.outputs_json = outputs_json
    await session.flush()
    return run


async def request_cancellation(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    result = await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if run is None:
        return None
    run.cancellation_requested = True
    run.status = "cancelled"
    run.completed_at = datetime.now(UTC)
    await session.flush()
    return run


async def count_active_runs_for_pipeline(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    include_pending: bool,
    exclude_run_id: uuid.UUID | None = None,
) -> int:
    """Count active runs for a pipeline.

    ``include_pending`` selects the behaviour (plan F3b — two behaviours
    instead of three):

    * ``include_pending=False`` (capacity gate): counts only runs that are
      actually executing or claimed (running/awaiting_human/claimed/
      waiting_for_lock) — a pending run does not hold capacity.
    * ``include_pending=True`` (variant-group quota): counts all non-terminal
      runs including ``pending``, preserving the 429 quota semantics.

    Optionally excludes a specific *run_id* from the count so a pending run does
    not count itself when checking capacity.
    """
    active_statuses = {"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"}
    if not include_pending:
        active_statuses = active_statuses - {"pending"}
    stmt = (
        select(func.count())
        .select_from(Run)
        .where(
            Run.pipeline_id == pipeline_id,
            Run.status.in_(active_statuses),
            Run.cancellation_requested == False,  # noqa: E712
        )
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    result = await session.execute(stmt)
    return int(result.scalar_one_or_none() or 0)


async def count_active_runs_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    include_pending: bool,
    exclude_run_id: uuid.UUID | None = None,
) -> int:
    """Count active runs for an organisation (org-level run concurrency gate).

    Mirrors :func:`count_active_runs_for_pipeline` but scopes to the WHOLE org
    across all pipelines. ``include_pending`` selects the same two behaviours:

    * ``include_pending=False`` (dispatch admission gate): counts only runs
      that are actually executing or claimed (running/awaiting_human/claimed/
      waiting_for_lock) — a pending run does not hold capacity.
    * ``include_pending=True`` (quota semantics): counts all non-terminal runs
      including ``pending``.

    ``exclude_run_id`` lets a pending run avoid counting itself. The explicit
    ``organisation_id`` filter makes the query cross-tenant safe on top of RLS
    (like :func:`count_active_sandbox_runs_for_org`) — a caller must still set
    RLS org context before invoking.
    """
    active_statuses = {"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"}
    if not include_pending:
        active_statuses = active_statuses - {"pending"}
    stmt = (
        select(func.count())
        .select_from(Run)
        .where(
            Run.organisation_id == org_id,
            Run.status.in_(active_statuses),
            Run.cancellation_requested == False,  # noqa: E712
        )
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    result = await session.execute(stmt)
    return int(result.scalar_one_or_none() or 0)


def _graph_contains_sandbox_agent(graph_json: dict[str, Any] | None) -> bool:
    """Top-level scan for any ``sandbox_agent`` node in a snapshot graph.

    Fail-open: ``None``, non-dicts, and missing ``nodes`` return ``False``
    (treat as non-sandbox, never block). Only the top-level ``nodes`` list is
    scanned — composite pipelines ARE compilable today: snapshots are expanded
    at creation time (``create_snapshot_from_live_graph``), so any sandbox
    sub-node of a composite template appears directly in the snapshot's
    top-level ``nodes`` and is found by this scan.
    """
    if not isinstance(graph_json, dict):
        return False
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return False
    return any(isinstance(n, dict) and n.get("node_type") == "sandbox_agent" for n in nodes)


async def count_active_sandbox_runs_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    exclude_run_id: uuid.UUID | None = None,
) -> int:
    """Count ``running`` sandbox-agent runs for an organisation.

    Only ``running`` runs whose snapshot graph contains a ``sandbox_agent``
    node count against the org sandbox cap. It is the sole executing state;
    pending, awaiting_human, and claimed runs hold no live sandbox — and
    neither do non-sandbox pipelines, so they must not consume a slot (B5).
    The explicit ``organisation_id`` filter makes the query cross-tenant safe
    on top of RLS; the snapshots join runs under the same RLS context.
    """
    stmt = (
        select(PipelineSnapshot.graph_json)
        .join(Run, Run.snapshot_id == PipelineSnapshot.id)
        .where(
            Run.organisation_id == org_id,
            Run.status == "running",
            Run.cancellation_requested == False,  # noqa: E712
        )
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    rows = (await session.execute(stmt)).scalars()
    return sum(1 for graph_json in rows if _graph_contains_sandbox_agent(graph_json))


async def get_sandbox_concurrency_limit(session: AsyncSession, org_id: uuid.UUID) -> int | None:
    """Read the org's sandbox concurrency limit from ``settings_json``.

    ``None`` means no cap. Fail-open: a malformed value (non-dict settings,
    string, float, bool) or a missing org returns ``None`` with a warning and
    never raises. An out-of-range ``int`` is clamped to ``[1, 100]`` so a
    direct-DB edit cannot crash the capacity claim.
    """
    org = await get_organisation(session, org_id)
    if org is None:
        _log.warning("sandbox_concurrency.org_not_found", extra={"org_id": str(org_id)})
        return None
    settings = org.settings_json
    if not isinstance(settings, dict):
        _log.warning("sandbox_concurrency.settings_not_dict", extra={"org_id": str(org_id)})
        return None
    raw = settings.get(_SANDBOX_CONCURRENCY_KEY)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        _log.warning(
            "sandbox_concurrency.invalid_type",
            extra={"org_id": str(org_id), "value": repr(raw)},
        )
        return None
    if raw < _SANDBOX_CONCURRENCY_MIN or raw > _SANDBOX_CONCURRENCY_MAX:
        _log.warning(
            "sandbox_concurrency.out_of_range",
            extra={"org_id": str(org_id), "value": raw},
        )
        return max(_SANDBOX_CONCURRENCY_MIN, min(_SANDBOX_CONCURRENCY_MAX, raw))
    return raw


async def get_org_run_concurrency_limit(session: AsyncSession, org_id: uuid.UUID) -> int | None:
    """Read the org's run concurrency limit from ``settings_json``.

    ``None`` means no cap. Fail-open: a malformed value (non-dict settings,
    string, float, bool) or a missing org returns ``None`` with a warning and
    never raises. An out-of-range ``int`` is clamped to ``[1, 100]`` so a
    direct-DB edit cannot crash the dispatch-time admission gate.
    """
    org = await get_organisation(session, org_id)
    if org is None:
        _log.warning("run_concurrency.org_not_found", extra={"org_id": str(org_id)})
        return None
    settings = org.settings_json
    if not isinstance(settings, dict):
        _log.warning("run_concurrency.settings_not_dict", extra={"org_id": str(org_id)})
        return None
    raw = settings.get(_RUN_CONCURRENCY_KEY)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        _log.warning(
            "run_concurrency.invalid_type",
            extra={"org_id": str(org_id), "value": repr(raw)},
        )
        return None
    if raw < _RUN_CONCURRENCY_MIN or raw > _RUN_CONCURRENCY_MAX:
        _log.warning(
            "run_concurrency.out_of_range",
            extra={"org_id": str(org_id), "value": raw},
        )
        return max(_RUN_CONCURRENCY_MIN, min(_RUN_CONCURRENCY_MAX, raw))
    return raw


def _percentile(sorted_data: list[float], p: float) -> float:
    """Linear interpolation percentile."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def _empty_run_stats() -> dict[str, Any]:
    """Zero-shaped stats response for an empty window (same shape as a full one)."""
    return {
        "total_runs": 0,
        "success_rate": 0.0,
        "avg_duration_ms": 0,
        "p50_duration_ms": 0,
        "p95_duration_ms": 0,
        "p99_duration_ms": 0,
        "runs_by_day": [],
        "failure_by_reason": [],
        "avg_duration_by_day": [],
    }


async def _get_dialect_name(session: AsyncSession) -> str:
    """Return the active SQLAlchemy dialect name (e.g. 'postgresql')."""
    bind = session.get_bind()
    if asyncio.iscoroutine(bind):
        bind = await bind
    return bind.dialect.name


async def get_run_stats(
    session: AsyncSession,
    period: str = "30d",
) -> dict[str, Any]:
    """Aggregated run stats for the given period (7d|30d|90d).

    Postgres computes the p50/p95/p99 duration percentiles in SQL via
    ``percentile_cont`` so the endpoint does not load every run in the window
    into Python. Generic backends (SQLite, MariaDB) fall back to loading runs
    and computing percentiles in Python because ``percentile_cont`` is
    Postgres-only. The response shape is identical on both paths.
    """
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    dialect = await _get_dialect_name(session)
    if dialect == "postgresql":
        return await _get_run_stats_postgres(session, cutoff)
    return await _get_run_stats_python(session, cutoff)


async def _get_run_stats_python(
    session: AsyncSession,
    cutoff: datetime,
) -> dict[str, Any]:
    """Generic-backend fallback: load runs into Python, compute percentiles locally."""
    result = await session.execute(
        select(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.created_at >= cutoff,
            Pipeline.deleted_at.is_(None),
        )
        .order_by(Run.created_at)
    )
    runs: list[Run] = list(result.scalars().all())

    total = len(runs)
    if total == 0:
        return _empty_run_stats()

    completed_runs = [r for r in runs if r.completed_at and r.started_at]
    durations_ms = sorted(
        int((r.completed_at - r.started_at).total_seconds() * 1000)
        for r in completed_runs
        if r.completed_at is not None and r.started_at is not None
    )

    success_count = sum(1 for r in runs if r.status == "complete")
    success_rate = round(success_count / total, 4)
    avg_duration = int(sum(durations_ms) / len(durations_ms)) if durations_ms else 0

    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "success": 0, "failed": 0})
    dur_by_day: dict[str, list[int]] = defaultdict(list)

    for r in runs:
        day = r.created_at.strftime("%Y-%m-%d")
        by_day[day]["count"] += 1
        if r.status == "complete":
            by_day[day]["success"] += 1
        elif r.status in ("failed", "cancelled", "eval_failed", "expired"):
            by_day[day]["failed"] += 1

    for r in completed_runs:
        day = r.created_at.strftime("%Y-%m-%d")
        if r.completed_at is None or r.started_at is None:
            continue
        ms = int((r.completed_at - r.started_at).total_seconds() * 1000)
        dur_by_day[day].append(ms)

    failure_reasons: dict[str, int] = defaultdict(int)
    for r in runs:
        if r.status in ("failed", "eval_failed") and r.error_code:
            failure_reasons[r.error_code] += 1

    return {
        "total_runs": total,
        "success_rate": success_rate,
        "avg_duration_ms": avg_duration,
        "p50_duration_ms": int(_percentile([float(x) for x in durations_ms], 50)),
        "p95_duration_ms": int(_percentile([float(x) for x in durations_ms], 95)),
        "p99_duration_ms": int(_percentile([float(x) for x in durations_ms], 99)),
        "runs_by_day": [{"date": d, **v} for d, v in sorted(by_day.items())],
        "failure_by_reason": [
            {"reason": r, "count": c} for r, c in sorted(failure_reasons.items(), key=lambda x: -x[1])
        ],
        "avg_duration_by_day": [{"date": d, "avg_ms": int(sum(v) / len(v))} for d, v in sorted(dur_by_day.items())],
    }


async def _get_run_stats_postgres(
    session: AsyncSession,
    cutoff: datetime,
) -> dict[str, Any]:
    """Postgres fast path: duration percentiles computed in SQL via ``percentile_cont``.

    RLS scoping still applies — the queries are ORM selects against ``Run`` and
    ``Pipeline`` and the route sets the org context with ``set_rls_org`` before
    calling this function. NULL durations are excluded from the percentile
    aggregates (a run without both ``started_at`` and ``completed_at`` has no
    duration); an empty percentile group yields ``None`` in the response.
    """
    duration_ms = func.extract("epoch", Run.completed_at - Run.started_at) * 1000
    base_where = (
        Run.created_at >= cutoff,
        Pipeline.deleted_at.is_(None),
    )
    day = cast(Run.created_at, Date).label("day")

    # Per-day count/success/failed buckets plus per-day average duration. A day
    # with runs but no completed durations has a NULL avg and is omitted, which
    # matches the generic path (days only appear once they have a duration).
    day_rows = list(
        (
            await session.execute(
                select(
                    day,
                    func.count().label("run_count"),
                    func.sum(case((Run.status == "complete", 1), else_=0)).label("success"),
                    func.sum(
                        case((Run.status.in_(("failed", "cancelled", "eval_failed", "expired")), 1), else_=0)
                    ).label("failed"),
                    func.avg(duration_ms).label("avg_duration"),
                )
                .select_from(Run)
                .join(Pipeline, Run.pipeline_id == Pipeline.id)
                .where(*base_where)
                .group_by(day)
            )
        ).all()
    )

    total = sum(int(row.run_count) for row in day_rows)
    if total == 0:
        return _empty_run_stats()

    # Whole-window duration percentiles + mean over completed runs (both
    # started_at and completed_at present). percentile_cont ignores NULLs and
    # returns NULL for an empty group, so the response null-guards below.
    overall = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(duration_ms).label("p50"),
                func.percentile_cont(0.95).within_group(duration_ms).label("p95"),
                func.percentile_cont(0.99).within_group(duration_ms).label("p99"),
                func.avg(duration_ms).label("avg_duration"),
            )
            .select_from(Run)
            .join(Pipeline, Run.pipeline_id == Pipeline.id)
            .where(
                *base_where,
                Run.completed_at.is_not(None),
                Run.started_at.is_not(None),
            )
        )
    ).one()
    p50 = overall.p50
    p95 = overall.p95
    p99 = overall.p99
    avg_duration = overall.avg_duration

    # Failure reason breakdown for failed / eval_failed runs carrying an error code.
    failure_rows = list(
        (
            await session.execute(
                select(Run.error_code, func.count())
                .select_from(Run)
                .join(Pipeline, Run.pipeline_id == Pipeline.id)
                .where(
                    *base_where,
                    Run.status.in_(("failed", "eval_failed")),
                    Run.error_code.is_not(None),
                    Run.error_code != "",
                )
                .group_by(Run.error_code)
            )
        ).all()
    )

    success_count = sum(int(row.success) for row in day_rows)
    success_rate = round(success_count / total, 4)

    return {
        "total_runs": total,
        "success_rate": success_rate,
        "avg_duration_ms": int(avg_duration) if avg_duration is not None else 0,
        "p50_duration_ms": int(p50) if p50 is not None else None,
        "p95_duration_ms": int(p95) if p95 is not None else None,
        "p99_duration_ms": int(p99) if p99 is not None else None,
        "runs_by_day": [
            {"date": str(row.day), "count": int(row.run_count), "success": int(row.success), "failed": int(row.failed)}
            for row in sorted(day_rows, key=attrgetter("day"))
        ],
        "failure_by_reason": [
            {"reason": reason, "count": int(count)}
            for reason, count in sorted(failure_rows, key=lambda item: -int(item[1]))
        ],
        "avg_duration_by_day": [
            {"date": str(row.day), "avg_ms": int(row.avg_duration)}
            for row in sorted(day_rows, key=attrgetter("day"))
            if row.avg_duration is not None
        ],
    }


async def get_run_heatmap(
    session: AsyncSession,
    year: int,
) -> list[dict[str, Any]]:
    """Run counts per day for the given year (for calendar heatmap)."""
    cutoff_start = datetime(year, 1, 1, tzinfo=UTC)
    cutoff_end = datetime(year + 1, 1, 1, tzinfo=UTC)

    result = await session.execute(
        select(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.created_at >= cutoff_start,
            Run.created_at < cutoff_end,
            Pipeline.deleted_at.is_(None),
        )
        .order_by(Run.created_at)
    )
    runs: list[Run] = list(result.scalars().all())

    by_day: dict[str, int] = defaultdict(int)
    for r in runs:
        by_day[r.created_at.strftime("%Y-%m-%d")] += 1

    return [{"date": d, "count": c} for d, c in sorted(by_day.items())]


async def batch_delete_old_terminal_runs(
    session: AsyncSession,
    *,
    max_age_days: int = 90,
    batch_size: int = 500,
) -> int:
    """Delete terminal runs older than *max_age_days* in batches.

    Only affects runs with status in (complete, failed, eval_failed, cancelled).
    Returns total deleted count.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    deleted_total = 0
    while True:
        ids = list(
            (
                await session.execute(
                    select(Run.id)
                    .where(
                        Run.status.in_(["complete", "failed", "eval_failed", "cancelled"]),
                        Run.created_at < cutoff,
                    )
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        await session.execute(delete(Run).where(Run.id.in_(ids)))
        deleted_total += len(ids)
        if len(ids) < batch_size:
            break
    return deleted_total


async def purge_runs(
    session: AsyncSession,
    *,
    older_than: str,
    batch_size: int = 500,
) -> dict[str, int]:
    """Delete terminal runs completed before *older_than* date, in batches.

    Requires RLS org context to be set by the caller.
    Returns dict with ``deleted_run_count``.
    """
    try:
        cutoff = datetime.strptime(older_than, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid date format: '{older_than}'. Expected YYYY-MM-DD.") from exc
    deleted_total = 0
    while True:
        ids = list(
            (
                await session.execute(
                    select(Run.id)
                    .where(
                        Run.status.in_(["complete", "failed", "eval_failed", "cancelled"]),
                        Run.completed_at < cutoff,
                    )
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        await session.execute(delete(Run).where(Run.id.in_(ids)))
        deleted_total += len(ids)
        if len(ids) < batch_size:
            break
    return {"deleted_run_count": deleted_total}


async def cancel_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    error_code: str = "cancelled",
) -> uuid.UUID | None:
    """Atomically cancel a run that is still in pending/running status."""
    result = await session.execute(
        text("""
            UPDATE runs
            SET status = 'failed',
                error_code = :error_code,
                completed_at = NOW()
            WHERE id = :run_id
              AND status IN ('running', 'pending')
            RETURNING id
        """),
        {"error_code": error_code, "run_id": run_id},
    )
    row = result.fetchone()
    if row:
        _log.warning("CRUD cancelled run %s with error_code=%s", run_id, error_code)
        return uuid.UUID(str(row[0]))
    return None
