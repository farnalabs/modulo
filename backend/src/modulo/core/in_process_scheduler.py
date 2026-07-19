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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.cleanup_jobs.webhook_dedup_cleanup import cleanup_scheduler_loop
from modulo.core.cron_scheduler import fire_cron_trigger
from modulo.core.trigger_engine.polling import fire_polling_trigger
from modulo.db.models.trigger import Trigger
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_POLL_INTERVAL = 30  # seconds between DB polls

# Engine reference set by start_schedulers(). Use dispose_scheduler_engine()
# on shutdown to clean up the connection pool.
_scheduler_engine: AsyncEngine | None = None

# Background worker reference set by set_bg_worker(). When set, cron and
# polling triggers submit their runs to the worker for execution.
_bg_worker: Any | None = None


def set_bg_worker(worker: Any) -> None:
    """Set the background worker reference for run submission."""
    global _bg_worker
    _bg_worker = worker


async def start_schedulers(
    engine: AsyncEngine | None = None,
) -> list[asyncio.Task[None]]:
    """Start all in-process scheduler loops.

    Args:
        engine: Optional pre-built async engine. When provided, the caller
            is responsible for disposing it on shutdown. When ``None``, a
            new engine is created and must be disposed via the returned
            engine reference (available as ``_scheduler_engine`` module var).

    Returns a list of ``asyncio.Task`` handles. The caller should cancel
    them on shutdown.
    """
    global _scheduler_engine
    settings = get_settings()
    _scheduler_engine = (
        engine
        if engine is not None
        else create_async_engine(
            settings.database_url,
            connect_args={"ssl": False, "statement_cache_size": 0} if settings.modulo_db == "postgres" else {},
        )
    )
    factory = async_sessionmaker(_scheduler_engine, expire_on_commit=False)

    tasks = [
        asyncio.create_task(_cron_scheduler_loop(factory), name="cron-scheduler"),
        asyncio.create_task(_polling_scheduler_loop(factory), name="polling-scheduler"),
        asyncio.create_task(cleanup_scheduler_loop(factory), name="cleanup-scheduler"),
    ]
    _log.info("In-process schedulers started — cron, polling, and cleanup tasks active")
    return tasks


_cron_tasks: set[asyncio.Task[None]] = set()
_polling_tasks: set[asyncio.Task[None]] = set()


async def _cron_scheduler_loop(factory: async_sessionmaker[AsyncSession]) -> None:
    """Poll for due cron triggers and fire them."""
    while True:
        try:
            due = await _fetch_due_cron_triggers(factory)
            for trigger_info in due:
                task = asyncio.create_task(
                    _fire_cron_wrapper(factory, trigger_info),
                    name=f"cron-fire-{trigger_info['id']}",
                )
                _cron_tasks.add(task)
                task.add_done_callback(_cron_tasks.discard)
        except asyncio.CancelledError:
            break
        except Exception:
            _log.exception("Cron scheduler loop error")
        await asyncio.sleep(_POLL_INTERVAL)


async def _polling_scheduler_loop(factory: async_sessionmaker[AsyncSession]) -> None:
    """Poll for due polling triggers and fire them."""
    while True:
        try:
            due = await _fetch_due_polling_triggers(factory)
            for trigger_info in due:
                task = asyncio.create_task(
                    _fire_polling_wrapper(factory, trigger_info),
                    name=f"polling-fire-{trigger_info['id']}",
                )
                _polling_tasks.add(task)
                task.add_done_callback(_polling_tasks.discard)
        except asyncio.CancelledError:
            break
        except Exception:
            _log.exception("Polling scheduler loop error")
        await asyncio.sleep(_POLL_INTERVAL)


async def _fetch_due_cron_triggers(factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """Query the database for cron triggers whose next_fire_at <= now."""
    now = datetime.datetime.now(datetime.UTC)
    triggers: list[dict[str, Any]] = []
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


async def _fetch_due_polling_triggers(factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """Query the database for polling triggers whose next_fire_at <= now."""
    now = datetime.datetime.now(datetime.UTC)
    triggers: list[dict[str, Any]] = []
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


async def _fire_cron_wrapper(factory: async_sessionmaker[AsyncSession], info: dict[str, Any]) -> None:
    """Fire one cron trigger — wrapper that logs outcomes and submits to bg worker."""
    try:
        result = await fire_cron_trigger(
            trigger_id=info["id"],
            org_id=info["org_id"],
            pipeline_id=info["pipeline_id"],
            snapshot_id=info["snapshot_id"],
            cron_expression=info["cron_expression"],
        )
        if result.get("status") == "fired":
            run_id = result.get("run_id")
            _log.info("In-process cron trigger %s → run %s", info["id"], run_id)
            if _bg_worker is not None and run_id is not None:
                _bg_worker.submit(
                    uuid.UUID(run_id),
                    info["org_id"],
                    result.get("input_payload", {}),
                )
                _log.info("In-process cron trigger %s → run %s submitted to worker", info["id"], run_id)
            elif _bg_worker is None:
                _log.warning(
                    "Cron trigger %s fired but background worker not available — run %s will not execute",
                    info["id"],
                    run_id,
                )
    except Exception:
        _log.exception("In-process cron trigger %s failed", info["id"])


async def _fire_polling_wrapper(factory: async_sessionmaker[AsyncSession], info: dict[str, Any]) -> None:
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


async def dispose_scheduler_engine() -> None:
    """Dispose the engine created by ``start_schedulers()``.

    Safe to call even if ``start_schedulers()`` was never called or if
    the engine was provided externally (in which case the caller owns it).
    """
    global _scheduler_engine
    if _scheduler_engine is not None:
        try:
            await _scheduler_engine.dispose()
            _log.info("Scheduler engine disposed")
        except Exception:
            _log.exception("Failed to dispose scheduler engine")
        _scheduler_engine = None
