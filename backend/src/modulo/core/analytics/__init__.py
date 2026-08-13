"""Analytics foundation — run_daily_facts live writer + shared helpers (ADR 020).

``record_run_facts`` is the LIVE writer: called from every terminal finalize
path (``cost_controller.finalize``) INSIDE the same transaction as the run
status write, AND — as a compensating row — from the SAQ task_failure hook
(``saq_hooks``) in its own separate session after the run is marked failed. It
NEVER raises (fail-open), NEVER feeds ``_fallback_write`` / ``_reduced_escape``,
and NEVER influences the cost result. A facts-write failure rolls back only the
fact (a savepoint), not the run's finalization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from modulo.core.analytics.metrics import record_facts_write_failed
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.team import Team

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

__all__ = ["TERMINAL_STATUSES", "compute_delta", "record_run_facts"]

# Re-export the single source of truth so consumers can import it from the
# analytics package root as well as from the model module.
TERMINAL_STATUSES = TERMINAL_STATUSES


def compute_delta(prev: float | None, curr: float | None) -> float | None:
    """Period-over-period percent change, rounded to 1dp.

    ``None`` when *prev* is zero/absent/non-finite (the change is infinite or
    undefined), when both are zero, or when no baseline exists. Negative
    deltas are returned as-is (a drop is a negative value — never clamped).
    """
    if prev is None or prev == 0:
        return None
    try:
        p = float(prev)
        c = float(curr) if curr is not None else 0.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or not math.isfinite(c):
        return None
    return round(((c - p) / abs(p)) * 100.0, 1)


def _fact_run_date(run: Run) -> date:
    """The UTC day a run is attributed to — identical to the ledger (ADR 020)."""
    base = run.started_at or run.created_at
    if base is None:
        return datetime.now(UTC).date()
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return base.astimezone(UTC).date()


def _fact_duration_ms(run: Run) -> int | None:
    if run.completed_at is not None and run.started_at is not None:
        return int((run.completed_at - run.started_at).total_seconds() * 1000)
    return None


def _fact_queue_wait_ms(run: Run) -> int | None:
    """Run.started_at - Run.dispatched_at when both present, else NULL.

    Semantics: ``dispatched_at`` is stamped by the dispatcher BEFORE the run is
    enqueued and ``started_at`` when a worker claims it, so
    ``dispatched_at < started_at`` in time and the stat is a POSITIVE queue
    wait. ``dispatched_at`` is read via ``getattr`` so any run-shaped object
    (legacy fakes without the attribute) degrades to NULL instead of raising.
    """
    dispatched_at = getattr(run, "dispatched_at", None)
    if dispatched_at is not None and run.started_at is not None:
        return int((run.started_at - dispatched_at).total_seconds() * 1000)
    return None


def _fact_total_queue_wait_ms(run: Run) -> int | None:
    """Run.started_at - Run.created_at in ms — the FULL queue wait (FAR-134).

    Unlike ``queue_wait_ms`` (started minus dispatched — the SAQ worker queue
    only), this covers the whole wait from run creation to execution start:
    capacity-deferral backoff PLUS the SAQ queue. NULL when either side is
    missing. ``created_at`` is read via ``getattr`` to mirror the file's
    defensive pattern for legacy run-shaped fakes.
    """
    created_at = getattr(run, "created_at", None)
    if created_at is not None and run.started_at is not None:
        return int((run.started_at - created_at).total_seconds() * 1000)
    return None


def _fact_final_idle_ms(run: Run) -> int | None:
    """Run.completed_at - Run.heartbeat_at — the stuck-with-no-heartbeat window.

    NULL when either side is absent. A NULL heartbeat_at with a completed run
    leaves the window unknowable, so the fact is NULL (never a negative value).
    ``heartbeat_at`` is read via ``getattr`` so any run-shaped object without
    the attribute degrades to NULL instead of raising.
    """
    heartbeat_at = getattr(run, "heartbeat_at", None)
    if run.completed_at is not None and heartbeat_at is not None:
        return int((run.completed_at - heartbeat_at).total_seconds() * 1000)
    return None


def _fact_output_bytes(run: Run) -> int | None:
    """Serialised size of Run.outputs_json (``json.dumps`` length) when present.

    Since FAR-125 P1 ``outputs_json`` holds PURE returns (telemetry excluded),
    so this fact measures the pure-return size. Historical values measured the
    pre-P1 envelope size and are NOT comparable across the P1 boundary —
    accepted, no fact backfill (pre-alpha). ``outputs_json`` is read via
    ``getattr`` so any run-shaped object without the attribute degrades to NULL
    instead of raising.
    """
    outputs_json = getattr(run, "outputs_json", None)
    if outputs_json is None:
        return None
    try:
        return len(json.dumps(outputs_json))
    except (TypeError, ValueError):
        return None


def _fact_telemetry_bytes(run: Run) -> int | None:
    """Serialised size of Run.node_telemetry_json (``json.dumps`` length) when present.

    Mirrors ``_fact_output_bytes``: NULL when the telemetry payload is absent
    and NULL (never a raise) when it cannot be serialised. ``node_telemetry_json``
    is read via ``getattr`` so any run-shaped object without the attribute
    degrades to NULL instead of raising.
    """
    node_telemetry_json = getattr(run, "node_telemetry_json", None)
    if node_telemetry_json is None:
        return None
    try:
        return len(json.dumps(node_telemetry_json))
    except (TypeError, ValueError):
        return None


def _derive_graph_dimensions(
    graph_json: Any,
) -> tuple[int, int, int | None]:
    """Node stats from a snapshot's ``graph_json`` — NULL-safe, mirroring
    ``derive_node_type_map`` (cost_controller.finalize).

    ``graph_json`` is the serialised pipeline graph: a dict with a ``nodes``
    list of dicts carrying ``node_type`` and ``timeout_seconds``. Malformed
    input degrades to ``(0, 0, None)`` instead of raising.
    """
    node_count = 0
    sandbox_agent_node_count = 0
    max_node_timeout_seconds: int | None = None
    if not isinstance(graph_json, dict):
        return node_count, sandbox_agent_node_count, max_node_timeout_seconds
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return node_count, sandbox_agent_node_count, max_node_timeout_seconds
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_count += 1
        if node.get("node_type") == "sandbox_agent":
            sandbox_agent_node_count += 1
        raw_timeout = node.get("timeout_seconds")
        if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool):
            timeout = int(raw_timeout)
            if max_node_timeout_seconds is None or timeout > max_node_timeout_seconds:
                max_node_timeout_seconds = timeout
    return node_count, sandbox_agent_node_count, max_node_timeout_seconds


async def _snapshot_dimensions(
    session: AsyncSession,
    run: Run,
) -> tuple[str | None, str | None, uuid.UUID | None]:
    """Team name + pipeline name/folder snapshots (explicit reads — async lazy-load is unavailable)."""
    team_name: str | None = None
    if run.owner_team_id is not None:
        team_name = (await session.execute(select(Team.name).where(Team.id == run.owner_team_id))).scalar_one_or_none()
    pipeline_name: str | None = None
    folder_id: uuid.UUID | None = None
    if run.pipeline_id is not None:
        row = (
            await session.execute(select(Pipeline.name, Pipeline.folder_id).where(Pipeline.id == run.pipeline_id))
        ).first()
        if row is not None:
            pipeline_name, folder_id = row
    return team_name, pipeline_name, folder_id


async def _snapshot_graph_dimensions(
    session: AsyncSession,
    run: Run,
) -> tuple[int, int, int | None]:
    """Snapshot node stats via an explicit SELECT — async lazy-load is unavailable.

    Reads ``graph_json`` from the ``pipeline_snapshots`` table by id (mirroring
    ``_snapshot_dimensions``), then derives the node stats NULL-safely. A
    missing/malformed snapshot degrades to ``(0, 0, None)``. ``snapshot_id`` is
    read via ``getattr`` so any run-shaped object without the attribute
    degrades to ``(0, 0, None)`` instead of raising.
    """
    snapshot_id = getattr(run, "snapshot_id", None)
    if snapshot_id is None:
        return _derive_graph_dimensions(None)
    graph_json = (
        await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == snapshot_id))
    ).scalar_one_or_none()
    return _derive_graph_dimensions(graph_json)


async def record_run_facts(session: AsyncSession, run: Run) -> None:
    """Upsert the daily fact for a terminal run — NEVER raises (fail-open).

    Wrapped in a savepoint so a facts-write failure rolls back only the fact
    and the outer transaction (the run finalization) survives. The upsert is
    ``INSERT ... ON CONFLICT (run_id) DO UPDATE`` — re-finalization corrects
    the fact in place.
    """
    try:
        team_name, pipeline_name, folder_id = await _snapshot_dimensions(session, run)
        node_count, sandbox_agent_node_count, max_node_timeout_seconds = await _snapshot_graph_dimensions(session, run)
        values: dict[str, Any] = {
            "run_id": run.id,
            "organisation_id": run.organisation_id,
            "run_date": _fact_run_date(run),
            "created_at": run.created_at,
            "team_id": run.owner_team_id,
            "team_name": team_name,
            "pipeline_id": run.pipeline_id,
            "pipeline_name": pipeline_name,
            "folder_id": folder_id,
            "trigger_type": run.trigger_type,
            "status": run.status,
            "total_cost_usd": run.total_cost_usd,
            "total_tokens": run.total_tokens,
            "duration_ms": _fact_duration_ms(run),
            "error_code": getattr(run, "error_code", None),
            "claim_count": getattr(run, "claim_count", None),
            "queue_wait_ms": _fact_queue_wait_ms(run),
            "final_idle_ms": _fact_final_idle_ms(run),
            "cancellation_requested": getattr(run, "cancellation_requested", None),
            "dispatcher": getattr(run, "dispatcher", None),
            "node_count": node_count,
            "sandbox_agent_node_count": sandbox_agent_node_count,
            "max_node_timeout_seconds": max_node_timeout_seconds,
            "parent_run_id": getattr(run, "parent_run_id", None),
            "snapshot_id": getattr(run, "snapshot_id", None),
            "run_number": getattr(run, "run_number", None),
            "output_bytes": _fact_output_bytes(run),
            "telemetry_bytes": _fact_telemetry_bytes(run),
            "rate_limited": getattr(run, "rate_limit_key", None) is not None,
            # FAR-134 concurrency columns — absolute run-lifecycle instants +
            # the full queue wait (started - created). getattr defensively for
            # legacy run-shaped objects that predate the Run fields.
            "dispatched_at": getattr(run, "dispatched_at", None),
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "total_queue_wait_ms": _fact_total_queue_wait_ms(run),
        }
        async with session.begin_nested():
            stmt = pg_insert(RunDailyFact).values(**values)
            update_cols = {
                "status": stmt.excluded.status,
                "total_cost_usd": stmt.excluded.total_cost_usd,
                "total_tokens": stmt.excluded.total_tokens,
                "trigger_type": stmt.excluded.trigger_type,
                "team_id": stmt.excluded.team_id,
                "team_name": stmt.excluded.team_name,
                "pipeline_id": stmt.excluded.pipeline_id,
                "pipeline_name": stmt.excluded.pipeline_name,
                "folder_id": stmt.excluded.folder_id,
                "run_date": stmt.excluded.run_date,
                "created_at": stmt.excluded.created_at,
                "duration_ms": stmt.excluded.duration_ms,
                "error_code": stmt.excluded.error_code,
                "claim_count": stmt.excluded.claim_count,
                "queue_wait_ms": stmt.excluded.queue_wait_ms,
                "final_idle_ms": stmt.excluded.final_idle_ms,
                "cancellation_requested": stmt.excluded.cancellation_requested,
                "dispatcher": stmt.excluded.dispatcher,
                "node_count": stmt.excluded.node_count,
                "sandbox_agent_node_count": stmt.excluded.sandbox_agent_node_count,
                "max_node_timeout_seconds": stmt.excluded.max_node_timeout_seconds,
                "parent_run_id": stmt.excluded.parent_run_id,
                "snapshot_id": stmt.excluded.snapshot_id,
                "run_number": stmt.excluded.run_number,
                "output_bytes": stmt.excluded.output_bytes,
                "telemetry_bytes": stmt.excluded.telemetry_bytes,
                "rate_limited": stmt.excluded.rate_limited,
                "dispatched_at": stmt.excluded.dispatched_at,
                "started_at": stmt.excluded.started_at,
                "completed_at": stmt.excluded.completed_at,
                "total_queue_wait_ms": stmt.excluded.total_queue_wait_ms,
            }
            await session.execute(stmt.on_conflict_do_update(index_elements=[RunDailyFact.run_id], set_=update_cols))
    except asyncio.CancelledError:
        raise
    except Exception:
        record_facts_write_failed()
        _log.warning(
            "analytics.facts.write_failed",
            extra={"run_id": str(run.id), "org_id": str(run.organisation_id), "status": run.status},
            exc_info=True,
        )
