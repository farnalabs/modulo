"""Cleanup job that deletes old runs based on retention policy."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
BATCH_SIZE = 500

_TERMINAL_STATES: tuple[str, ...] = ("complete", "failed", "eval_failed", "cancelled")


async def cleanup_old_runs(
    db_session: AsyncSession,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Delete completed/failed runs older than *retention_days*.

    Only affects runs in terminal states (complete, failed, eval_failed, cancelled).
    Uses batched select-then-delete to avoid long-held row locks.
    The caller is responsible for setting up any required RLS context.

    Returns the number of deleted runs.
    """
    if retention_days < 1:
        raise ValueError(f"retention_days must be >= 1, got {retention_days}")

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    total = 0

    while True:
        result = await db_session.execute(
            select(Run.id)
            .where(
                Run.status.in_(_TERMINAL_STATES),
                Run.created_at < cutoff,
            )
            .order_by(Run.id)
            .limit(BATCH_SIZE)
        )
        ids = result.scalars().all()
        if not ids:
            break

        await db_session.execute(delete(Run).where(Run.id.in_(ids)))
        try:
            await db_session.commit()
        except Exception:
            _log.exception("Failed to commit run retention cleanup for %d runs", len(ids))
            raise
        total += len(ids)

        if len(ids) < BATCH_SIZE:
            break

    if total > 0:
        _log.info("Cleaned up %d old runs (retention: %d days)", total, retention_days)
    return total
