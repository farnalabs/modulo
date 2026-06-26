"""In-process asyncio schedulers for cron and polling triggers.

Started by the application lifespan when Redis/Celery is not configured.
Each scheduler runs as an ``asyncio.Task`` that polls the database for due
triggers and fires them directly — no Celery or Redis required.

For multi-replica deployments, configure Redis — the Celery beat scheduler
coordinates across workers to prevent duplicate firings.
"""

import asyncio
import datetime
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.cron_scheduler import fire_cron_trigger
from modulo.core.trigger_engine.polling import fire_polling_trigger
from modulo.db.models.trigger import Trigger
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_POLL_INTERVAL = 30  # seconds between DB polls


async def start_schedulers() -> list[asyncio.Task]:
    """Start all in-process scheduler loops.

    Returns a list of ``asyncio.Task`` handles. The caller should cancel
    them on shutdown.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    tasks = [
        asyncio.create_task(_cron_scheduler_loop(factory), name="cron-scheduler"),
        asyncio.create_task(_polling_scheduler_loop(factory), name="polling-scheduler"),
    ]
    _log.info("In-process schedulers started — cron and polling triggers active")
    return tasks


async def _cron_scheduler_loop(factory: async_sessionmaker) -> None:
    """Poll for due cron triggers and fire them."""
    while True:
        try:
            due = await _fetch_due_cron_triggers(factory)
            for trigger_info in due:
                asyncio.create_task(
                    _fire_cron_wrapper(factory, trigger_info),
                    name=f"cron-fire-{trigger_info['id']}",
                )
        except asyncio.CancelledError:
            break
        except Exception:
            _log.exception("Cron scheduler loop error")
        await asyncio.sleep(_POLL_INTERVAL)


async def _polling_scheduler_loop(factory: async_sessionmaker) -> None:
    """Poll for due polling triggers and fire them."""
    while True:
        try:
            due = await _fetch_due_polling_triggers(factory)
            for trigger_info in due:
                asyncio.create_task(
                    _fire_polling_wrapper(factory, trigger_info),
                    name=f"polling-fire-{trigger_info['id']}",
                )
        except asyncio.CancelledError:
            break
        except Exception:
            _log.exception("Polling scheduler loop error")
        await asyncio.sleep(_POLL_INTERVAL)


async def _fetch_due_cron_triggers(factory: async_sessionmaker) -> list[dict]:
    """Query the database for cron triggers whose next_fire_at <= now."""
    now = datetime.datetime.now(datetime.UTC)
    triggers: list[dict] = []
    async with factory() as session:
        result = await session.execute(
            select(
                Trigger.id,
                Trigger.organisation_id,
                Trigger.pipeline_id,
                Trigger.config_json,
                Trigger.cron_expression,
                Trigger.next_fire_at,
            ).where(
                Trigger.trigger_type == "cron",
                Trigger.active == True,  # noqa: E712
                Trigger.next_fire_at <= now,
                Trigger.cron_expression.isnot(None),
            )
        )
        for row in result.all():
            config = row.config_json or {}
            snapshot_id_str = config.get("snapshot_id")
            try:
                snapshot_id = uuid.UUID(snapshot_id_str) if snapshot_id_str else uuid.uuid4()
            except (ValueError, TypeError):
                snapshot_id = uuid.uuid4()
            triggers.append(
                {
                    "id": row.id,
                    "org_id": row.organisation_id,
                    "pipeline_id": row.pipeline_id,
                    "snapshot_id": snapshot_id,
                    "cron_expression": row.cron_expression,
                    "next_fire_at": row.next_fire_at,
                }
            )
    return triggers


async def _fetch_due_polling_triggers(factory: async_sessionmaker) -> list[dict]:
    """Query the database for polling triggers whose next_fire_at <= now."""
    now = datetime.datetime.now(datetime.UTC)
    triggers: list[dict] = []
    async with factory() as session:
        result = await session.execute(
            select(
                Trigger.id,
                Trigger.organisation_id,
                Trigger.pipeline_id,
                Trigger.config_json,
                Trigger.next_fire_at,
            ).where(
                Trigger.trigger_type == "polling",
                Trigger.active == True,  # noqa: E712
                Trigger.next_fire_at <= now,
            )
        )
        for row in result.all():
            config = row.config_json or {}
            ci_id_str = config.get("connector_instance_id")
            try:
                connector_instance_id = uuid.UUID(ci_id_str) if ci_id_str else None
            except (ValueError, TypeError):
                connector_instance_id = None
            if connector_instance_id is None:
                _log.warning("Polling trigger %s has no connector_instance_id", row.id)
                continue

            snapshot_id_str = config.get("snapshot_id")
            try:
                snapshot_id = uuid.UUID(snapshot_id_str) if snapshot_id_str else uuid.uuid4()
            except (ValueError, TypeError):
                snapshot_id = uuid.uuid4()

            triggers.append(
                {
                    "id": row.id,
                    "org_id": row.organisation_id,
                    "pipeline_id": row.pipeline_id,
                    "snapshot_id": snapshot_id,
                    "connector_instance_id": connector_instance_id,
                    "poll_query": config.get("poll_query", ""),
                    "condition_expression": config.get("condition_expression"),
                    "next_fire_at": row.next_fire_at,
                }
            )
    return triggers


async def _fire_cron_wrapper(factory: async_sessionmaker, info: dict) -> None:
    """Fire one cron trigger — wrapper that logs outcomes."""
    try:
        result = await fire_cron_trigger(
            trigger_id=info["id"],
            org_id=info["org_id"],
            pipeline_id=info["pipeline_id"],
            snapshot_id=info["snapshot_id"],
            cron_expression=info["cron_expression"],
        )
        if result.get("status") == "fired":
            _log.info("In-process cron trigger %s → run %s", info["id"], result.get("run_id"))
    except Exception:
        _log.exception("In-process cron trigger %s failed", info["id"])


async def _fire_polling_wrapper(factory: async_sessionmaker, info: dict) -> None:
    """Fire one polling trigger — wrapper that logs outcomes."""
    try:
        result = await fire_polling_trigger(
            trigger_id=info["id"],
            org_id=info["org_id"],
            pipeline_id=info["pipeline_id"],
            connector_instance_id=info["connector_instance_id"],
            poll_query=info["poll_query"],
            condition_expression=info.get("condition_expression"),
        )
        if result.get("status") == "fired":
            _log.info("In-process polling trigger %s → run %s", info["id"], result.get("run_id"))
    except Exception:
        _log.exception("In-process polling trigger %s failed", info["id"])
