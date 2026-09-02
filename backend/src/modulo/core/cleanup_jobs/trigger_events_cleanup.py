"""Cleanup job that removes old trigger_events rows to prevent unbounded growth.

``trigger_events`` previously had no age-based retention of its own: the webhook
dedup cleanup only prunes events older than its narrower 30-day window, and
``batch_delete_old_terminal_runs`` deletes runs only (``trigger_events.run_id``
is ``ON DELETE SET NULL``, so every event row survives run purge). Cron,
polling, webhook, agent_signal and ongoing triggers all write event rows, so
the table grows without bound in production.

This job deletes events whose ``received_at`` is older than a retention window
(default 90 days — aligned with the run-retention policy in
``batch_delete_old_terminal_runs`` / the analytics ``_RUN_RETENTION_DAYS``, and
comfortably longer than the webhook replay window, so events needed for replay
are never purged) in bounded batches. Mirrors ``webhook_dedup_cleanup``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.trigger_event import TriggerEvent

_log = logging.getLogger(__name__)

# 90 days aligns with the run-retention policy (the ``batch_delete_old_terminal_runs``
# default and the analytics ``_RUN_RETENTION_DAYS``) and exceeds the 30-day webhook
# replay window — replayable events are never purged by this job.
DEFAULT_RETENTION_DAYS = 90
BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# Core cleanup function
# ---------------------------------------------------------------------------


async def cleanup_old_trigger_events(
    db_session: AsyncSession,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Delete trigger_events received more than *retention_days* ago.

    Uses a two-step select-then-delete pattern (matching the existing cleanup
    in ``webhook_dedup_cleanup``) to safely batch-delete without holding
    long-lived row locks. Returns the number of deleted rows.
    """
    if retention_days < 1:
        raise ValueError(f"retention_days must be >= 1, got {retention_days}")
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    result = await db_session.execute(
        select(TriggerEvent.id).where(TriggerEvent.received_at < cutoff).order_by(TriggerEvent.id).limit(BATCH_SIZE)
    )
    ids = result.scalars().all()
    if not ids:
        return 0

    await db_session.execute(delete(TriggerEvent).where(TriggerEvent.id.in_(ids)))
    try:
        await db_session.commit()
    except Exception:
        _log.exception("Failed to commit trigger_events cleanup for %d events", len(ids))
        raise

    _log.info("Cleaned up %d old trigger_events", len(ids))
    return len(ids)
