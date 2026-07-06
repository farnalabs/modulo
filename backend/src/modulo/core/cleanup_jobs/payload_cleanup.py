"""Cleanup job that removes retained input/output payloads from runs."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
BATCH_SIZE = 500

_TERMINAL_STATES: tuple[str, ...] = ("complete", "failed", "eval_failed", "cancelled")


async def cleanup_retained_payloads(
    db_session: AsyncSession,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Null out retained payloads for runs older than *retention_days*.

    Affects the ``input_payload`` and ``outputs_json`` columns on runs
    whose ``created_at`` is before the computed cutoff.  Uses batched
    select-then-update to avoid long-held row locks.
    The caller is responsible for setting up any required RLS context.

    Returns the number of rows updated.
    """
    if retention_days < 1:
        raise ValueError(f"retention_days must be >= 1, got {retention_days}")

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    total = 0

    while True:
        result = await db_session.execute(
            select(Run.id)
            .where(
                Run.created_at < cutoff,
                Run.status.in_(_TERMINAL_STATES),
                (Run.input_payload.isnot(None) | Run.outputs_json.isnot(None)),
            )
            .limit(BATCH_SIZE)
        )
        ids = result.scalars().all()
        if not ids:
            break

        await db_session.execute(
            update(Run).where(Run.id.in_(ids)).values(input_payload=None, outputs_json=None)
        )
        await db_session.commit()
        total += len(ids)

        if len(ids) < BATCH_SIZE:
            break

    if total > 0:
        _log.info("Cleaned up retained payloads for %d runs (retention: %d days)", total, retention_days)
    return total
