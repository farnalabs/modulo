"""Analytics foundation — run_daily_facts live writer + shared helpers (ADR 020).

``record_run_facts`` is the LIVE writer: called from every terminal finalize
path (``cost_controller.finalize``) INSIDE the same transaction as the run
status write. It NEVER raises (fail-open), NEVER feeds ``_fallback_write`` /
``_reduced_escape``, and NEVER influences the cost result. A facts-write
failure rolls back only the fact (a savepoint), not the run's finalization.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from modulo.core.analytics.metrics import record_facts_write_failed
from modulo.db.models.pipeline import Pipeline
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


async def record_run_facts(session: AsyncSession, run: Run) -> None:
    """Upsert the daily fact for a terminal run — NEVER raises (fail-open).

    Wrapped in a savepoint so a facts-write failure rolls back only the fact
    and the outer transaction (the run finalization) survives. The upsert is
    ``INSERT ... ON CONFLICT (run_id) DO UPDATE`` — re-finalization corrects
    the fact in place.
    """
    try:
        team_name, pipeline_name, folder_id = await _snapshot_dimensions(session, run)
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
