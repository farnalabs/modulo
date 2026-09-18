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


# ---------------------------------------------------------------------------
# Expired dedup-hash purge (FAR-661)
# ---------------------------------------------------------------------------

# Every webhook delivery leaves one WebhookDedupHash row with a 5-minute TTL
# (``TriggerEngine._DEDUP_TTL_SECONDS``); this purge bounds how long expired
# rows survive past their TTL. Batched so a pile-up (10k+ rows observed in
# manual testing) drains without holding a long-lived lock on the table.
DEDUP_PURGE_BATCH_SIZE = 1000


async def purge_expired_dedup_hashes(
    db_session: AsyncSession,
    batch_size: int = DEDUP_PURGE_BATCH_SIZE,
) -> int:
    """Delete ONE BATCH of expired ``webhook_dedup_hashes`` rows.

    FAR-661: the hourly ``webhook_dedup_cleanup`` cron only retained
    ``trigger_events`` — nothing scheduled ever deleted the dedup rows
    themselves, so they accumulated forever and only surfaced as housekeeping
    candidates. This helper gives the SAQ system cron a bounded purge: a
    single select-then-delete pass capped at *batch_size* rows that delegates
    to ``TriggerEngine.cleanup_expired_dedup_hashes`` (reuse, not a
    re-implementation) so the Postgres advisory lock (key=20250601) is
    honoured — when a concurrent cleanup (e.g. the manual
    ``POST /triggers/cleanup-expired`` route) holds the lock, the pass
    returns 0 and the caller's drain loop stops until the next tick.

    One batch per call (mirroring ``cleanup_old_webhook_events``): the SAQ
    wrapper opens a fresh transaction per pass so no long-lived lock is held
    on the table, and this helper commits at the end of each pass. Returns
    the number of rows deleted in this pass.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")

    from modulo.core.trigger_engine import TriggerEngine

    deleted = await TriggerEngine.cleanup_expired_dedup_hashes(db_session, limit=batch_size)
    if not deleted:
        return 0

    try:
        await db_session.commit()
    except Exception:
        _log.exception("Failed to commit expired dedup hash purge for %d rows", deleted)
        raise

    _log.info("Purged %d expired webhook dedup hashes", deleted)
    return deleted
