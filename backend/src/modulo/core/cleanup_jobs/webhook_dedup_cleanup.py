"""Cleanup job that removes old webhook trigger events to prevent table bloat."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.trigger_event import TriggerEvent

_log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
BATCH_SIZE = 1000

# ---------------------------------------------------------------------------
# Core cleanup function
# ---------------------------------------------------------------------------


async def cleanup_old_webhook_events(
    db_session: AsyncSession,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Delete webhook trigger events older than *retention_days*.

    Uses a two-step select-then-delete pattern (matching the existing
    cleanup in ``TriggerEngine``) to safely batch-delete without
    holding long-lived row locks. Returns the number of deleted rows.
    """
    if retention_days < 1:
        raise ValueError(f"retention_days must be >= 1, got {retention_days}")
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    result = await db_session.execute(
        select(TriggerEvent.id).where(TriggerEvent.created_at < cutoff).order_by(TriggerEvent.id).limit(BATCH_SIZE)
    )
    ids = result.scalars().all()
    if not ids:
        return 0

    await db_session.execute(delete(TriggerEvent).where(TriggerEvent.id.in_(ids)))
    try:
        await db_session.commit()
    except Exception:
        _log.exception("Failed to commit webhook dedup cleanup for %d events", len(ids))
        raise

    _log.info("Cleaned up %d old webhook trigger events", len(ids))
    return len(ids)
