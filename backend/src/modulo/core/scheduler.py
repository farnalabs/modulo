"""Minimal in-process cron scheduler — fires due triggers in an asyncio loop."""
import asyncio
import logging
from datetime import datetime, timezone

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
    try:
        settings = get_settings()
        engine = create_async_engine(
            settings.database_url,
            pool_size=1,
            max_overflow=2,
            pool_pre_ping=True,
        )
    except Exception as exc:
        _log.error("Cron scheduler failed to create engine: %s", exc)
        return

    try:
        while True:
            try:
                if stop_event.is_set():
                    break
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
                        await fire_cron_trigger(
                            trigger_id=trigger.id,
                            org_id=trigger.organisation_id,
                            pipeline_id=trigger.pipeline_id,
                            cron_expression=trigger.cron_expression,
                            snapshot_id=trigger.config_json.get("snapshot_id") if trigger.config_json else None,
                            factory=factory,
                        )
            except asyncio.CancelledError:
                _log.info("Cron scheduler cancelled")
                break
            except Exception:
                _log.exception("Cron scheduler iteration failed")
            try:
                if stop_event.is_set():
                    break
                await asyncio.wait_for(asyncio.sleep(_POLL_INTERVAL), timeout=_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        _log.error("Cron scheduler unexpected error: %s", exc)
    finally:
        await engine.dispose()
        _log.info("Cron scheduler stopped")
