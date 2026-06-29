"""Cleanup job that deletes old runs based on retention policy."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
BATCH_SIZE = 500

_TERMINAL_STATES = ("complete", "failed", "eval_failed", "cancelled")


async def cleanup_old_runs(
    db_session: AsyncSession,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Delete completed/failed runs older than *retention_days*.

    Only affects runs in terminal states (complete, failed, eval_failed, cancelled).
    The caller is responsible for setting up any required RLS context.

    Returns the number of deleted runs.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    result = await db_session.execute(
        delete(Run)
        .where(
            Run.status.in_(_TERMINAL_STATES),
            Run.created_at < cutoff,
        )
    )
    await db_session.commit()

    count = result.rowcount
    if count > 0:
        _log.info("Cleaned up %d old runs (retention: %d days)", count, retention_days)
    return count
