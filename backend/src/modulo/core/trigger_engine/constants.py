"""Shared constants for trigger and run lifecycle."""

import uuid
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent

# Active statuses — runs in these states count toward concurrency limits
_ACTIVE_STATUSES: Final[tuple[str, ...]] = (
    "running",
    "pending",
    "awaiting_human",
    "claimed",
    "waiting_for_lock",
)


async def count_active_runs(
    session: AsyncSession,
    *,
    trigger_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
) -> int:
    """Count active runs, optionally filtered by trigger or pipeline."""
    query = select(func.count()).where(
        Run.status.in_(_ACTIVE_STATUSES),
        Run.cancellation_requested == False,  # noqa: E712
    )
    if trigger_id is not None:
        query = query.where(Run.trigger_id == trigger_id)
    if pipeline_id is not None:
        query = query.where(Run.pipeline_id == pipeline_id)
    result = await session.execute(query)
    return int(result.scalar_one() or 0)


async def log_trigger_event(
    session: AsyncSession,
    *,
    trigger: Trigger,
    org_id: uuid.UUID,
    trigger_type: str,
    payload_hash: str,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> TriggerEvent:
    """Create a TriggerEvent row."""
    event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type=trigger_type,
        raw_payload_hash=payload_hash,
        validation_result=result,
        run_id=run_id,
        error_detail=error_detail,
    )
    session.add(event)
    await session.flush()
    return event
