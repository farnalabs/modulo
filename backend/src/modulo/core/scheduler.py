"""Minimal in-process cron scheduler — fires due triggers in an asyncio loop."""
import asyncio
from datetime import datetime, timezone
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.cron_scheduler import fire_cron_trigger
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_POLL_INTERVAL = 30


async def run_scheduler(stop_event: asyncio.Event) -> None:
    """Poll for due cron triggers and fire them."""
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_size=1,
        max_overflow=2,
        pool_pre_ping=True,
    )
    try:
        while not stop_event.is_set():
            try:
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session, session.begin():
                    await set_rls_org(session, "00000000-0000-0000-0000-000000000000")
                    now = datetime.now(timezone.utc)
                    result = await session.execute(
                        select(Trigger).where(
                            Trigger.trigger_type == "cron",
                            Trigger.active.is_(True),
                            Trigger.next_fire_at <= now,
                        )
                    )
                    for trigger in result.scalars():
                        await fire_cron_trigger(session, trigger)
            except Exception:
                _log.exception("Scheduler iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass  # Expected — time to poll again
    finally:
        await engine.dispose()
        _log.info("Cron scheduler stopped")

