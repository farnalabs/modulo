"""Cleanup job that removes old webhook trigger events to prevent table bloat."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import Celery, Task  # type: ignore[import-untyped]
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.db.models.trigger_event import TriggerEvent
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

RETENTION_DAYS = 30
BATCH_SIZE = 1000

# ---------------------------------------------------------------------------
# Core cleanup function
# ---------------------------------------------------------------------------


async def cleanup_old_webhook_events(db_session: AsyncSession) -> int:
    """Delete webhook trigger events older than RETENTION_DAYS.

    Uses a two-step select-then-delete pattern (matching the existing
    cleanup in ``TriggerEngine``) to safely batch-delete without
    holding long-lived row locks. Returns the number of deleted rows.
    """
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)

    result = await db_session.execute(
        select(TriggerEvent.id).where(TriggerEvent.created_at < cutoff).limit(BATCH_SIZE)
    )
    ids = result.scalars().all()
    if not ids:
        return 0

    await db_session.execute(delete(TriggerEvent).where(TriggerEvent.id.in_(ids)))
    await db_session.commit()

    count = len(ids)
    if count > 0:
        _log.info("Cleaned up %d old webhook trigger events", count)
    return count


# ---------------------------------------------------------------------------
# Celery task — wraps cleanup_old_webhook_events for Celery beat
# ---------------------------------------------------------------------------

CELERY_APP_GLOBAL: Celery | None = None


def get_celery_app() -> Celery:
    global CELERY_APP_GLOBAL
    if CELERY_APP_GLOBAL is None:
        from modulo.celery_app import celery_app as _app

        CELERY_APP_GLOBAL = _app
    return CELERY_APP_GLOBAL


_ENGINE_GLOBAL: Any = None


def _get_engine() -> Any:
    global _ENGINE_GLOBAL
    if _ENGINE_GLOBAL is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        _ENGINE_GLOBAL = create_async_engine(get_settings().database_url)
    return _ENGINE_GLOBAL


class WebhookDedupCleanupTask(Task):  # type: ignore[misc]
    """Celery task that runs the webhook dedup cleanup once per hour."""

    name = "modulo.cleanup.webhook_dedup"
    autoretry_for = (Exception,)
    max_retries = 3
    default_retry_delay = 300

    def run(self) -> dict[str, Any]:
        """Run one iteration of cleanup, batching until fewer than BATCH_SIZE rows remain."""
        import asyncio

        return asyncio.run(_run_cleanup())


async def _run_cleanup() -> dict[str, Any]:
    """Execute cleanup in batches until the table is under the retention threshold."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = _get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    total = 0
    async with factory() as session:
        while True:
            deleted = await cleanup_old_webhook_events(session)
            total += deleted
            if deleted < BATCH_SIZE:
                break
    _log.info("Webhook dedup cleanup complete — %d rows deleted", total)
    return {"deleted": total}


# ---------------------------------------------------------------------------
# In-process scheduler loop
# ---------------------------------------------------------------------------

_CLEANUP_INTERVAL_SECONDS = 3600  # run once per hour


async def cleanup_scheduler_loop(factory: async_sessionmaker) -> None:
    """Periodic background loop that purges old webhook trigger events.

    Runs every ``_CLEANUP_INTERVAL_SECONDS``. Intended to be started as an
    ``asyncio.Task`` alongside the cron/polling scheduler loops.
    """
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            total = 0
            async with factory() as session:
                while True:
                    deleted = await cleanup_old_webhook_events(session)
                    total += deleted
                    if deleted < BATCH_SIZE:
                        break
            if total > 0:
                _log.info("Scheduled cleanup removed %d old webhook trigger events", total)
        except asyncio.CancelledError:
            break
        except Exception:
            _log.exception("Webhook dedup cleanup loop error")
