"""Cleanup job that removes retained input/output payloads from runs."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
BATCH_SIZE = 500


async def cleanup_retained_payloads(
    db_session: AsyncSession,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Null out retained payloads for runs older than *retention_days*.

    Affects the ``input_payload`` and ``outputs_json`` columns on runs
    whose ``created_at`` is before the computed cutoff.  The caller is
    responsible for setting up any required RLS context.

    Returns the number of rows updated.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    result = await db_session.execute(
        update(Run)
        .where(Run.created_at < cutoff)
        .values(input_payload=None, outputs_json=None)
    )
    await db_session.commit()

    count = result.rowcount
    if count > 0:
        _log.info("Cleaned up retained payloads for %d runs (retention: %d days)", count, retention_days)
    return count
