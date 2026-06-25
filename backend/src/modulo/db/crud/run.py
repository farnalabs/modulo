"""CRUD for Run records.

All functions require RLS org context to be set by the caller.
"""

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.models.run import Run


def _input_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 hex digest of a JSON-serialisable payload."""
    serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()


async def create_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    trigger_type: str,
    input_payload: dict[str, Any],
    created_by: uuid.UUID | None = None,
    trigger_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> Run:
    run_id = uuid.uuid4()
    thread_id = f"{org_id}:{run_id}"
    run = Run(
        id=run_id,
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type=trigger_type,
        input_hash=_input_hash(input_payload),
        created_by=created_by,
        trigger_id=trigger_id,
        owner_team_id=owner_team_id,
        langgraph_thread_id=thread_id,
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
    run = await get_run(session, run_id)
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
    page: int = 1,
    page_size: int = 20,
) -> PageResult[Run]:
    q = select(Run)
    count_q = select(func.count()).select_from(Run)
    if pipeline_id is not None:
        q = q.where(Run.pipeline_id == pipeline_id)
        count_q = count_q.where(Run.pipeline_id == pipeline_id)
    if status is not None:
        q = q.where(Run.status == status)
        count_q = count_q.where(Run.status == status)

    offset = (page - 1) * page_size
    total = (await session.execute(count_q)).scalar_one()
    items = list((await session.execute(q.order_by(Run.created_at.desc()).offset(offset).limit(page_size))).scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_run_status(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: str,
    *,
    error_code: str | None = None,
    error_detail: str | None = None,
    total_tokens: int | None = None,
    total_cost_usd: Decimal | None = None,
) -> Run | None:
    run = await get_run(session, run_id)
    if run is None:
        return None
    run.status = status
    if status == "running" and run.started_at is None:
        run.started_at = datetime.now(UTC)
    if status in ("complete", "failed", "cancelled", "eval_failed"):
        run.completed_at = datetime.now(UTC)
    if error_code is not None:
        run.error_code = error_code
    if error_detail is not None:
        run.error_detail = error_detail
    if total_tokens is not None:
        run.total_tokens = total_tokens
    if total_cost_usd is not None:
        run.total_cost_usd = total_cost_usd
    await session.flush()
    return run


async def request_cancellation(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    run = await get_run(session, run_id)
    if run is None:
        return None
    run.cancellation_requested = True
    await session.flush()
    return run


async def count_active_runs_for_pipeline(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> int:
    """Count runs in non-terminal states for a given pipeline."""
    active_statuses = {"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"}
    result = await session.execute(
        select(func.count())
        .select_from(Run)
        .where(
            Run.pipeline_id == pipeline_id,
            Run.status.in_(active_statuses),
        )
    )
    return int(result.scalar_one())


def _percentile(sorted_data: list[float], p: float) -> float:
    """Linear interpolation percentile."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


async def get_run_stats(
    session: AsyncSession,
    period: str = "30d",
) -> dict[str, Any]:
    """Aggregated run stats for the given period (7d|30d|90d)."""
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    result = await session.execute(select(Run).where(Run.created_at >= cutoff).order_by(Run.created_at))
    runs: list[Run] = list(result.scalars().all())

    total = len(runs)
    if total == 0:
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

    completed_runs = [r for r in runs if r.completed_at and r.started_at]
    durations_ms = sorted(int((r.completed_at - r.started_at).total_seconds() * 1000) for r in completed_runs)

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
        else:
            by_day[day]["failed"] += 1

    for r in completed_runs:
        day = r.created_at.strftime("%Y-%m-%d")
        ms = int((r.completed_at - r.started_at).total_seconds() * 1000)
        dur_by_day[day].append(ms)

    failure_reasons: dict[str, int] = defaultdict(int)
    for r in runs:
        if r.status == "failed" and r.error_code:
            failure_reasons[r.error_code] += 1

    return {
        "total_runs": total,
        "success_rate": success_rate,
        "avg_duration_ms": avg_duration,
        "p50_duration_ms": int(_percentile(durations_ms, 50)),
        "p95_duration_ms": int(_percentile(durations_ms, 95)),
        "p99_duration_ms": int(_percentile(durations_ms, 99)),
        "runs_by_day": [{"date": d, **v} for d, v in sorted(by_day.items())],
        "failure_by_reason": [
            {"reason": r, "count": c} for r, c in sorted(failure_reasons.items(), key=lambda x: -x[1])
        ],
        "avg_duration_by_day": [{"date": d, "avg_ms": int(sum(v) / len(v))} for d, v in sorted(dur_by_day.items())],
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
        .where(
            Run.created_at >= cutoff_start,
            Run.created_at < cutoff_end,
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

    Only affects runs with status in (complete, failed, cancelled).
    Returns total deleted count.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    deleted_total = 0
    while True:
        ids = list(
            (
                await session.execute(
                    select(Run.id)
                    .where(
                        Run.status.in_(["complete", "failed", "cancelled"]),
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
    return deleted_total
