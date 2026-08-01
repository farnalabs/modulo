"""Minimal in-process cron scheduler — fires due triggers in an asyncio loop."""

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.cron_helpers import fire_cron_trigger
from modulo.core.dispatch import dispatch_run
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_POLL_INTERVAL = 30


async def _run_scheduler_loop(
    stop_event: asyncio.Event,
    bg_worker: Any | None = None,
) -> None:
    """Core polling loop — runs inside the scheduler task.

    Creates its own engine so a connection failure never takes down the
    whole loop (the outer ``run_scheduler`` restarts on unexpected exit).
    """
    try:
        settings = get_settings()
        db_type = settings.modulo_db.lower()
        connect_args: dict[str, Any] = {"timeout": 10}
        if db_type == "postgres":
            connect_args["ssl"] = False
            connect_args["statement_cache_size"] = 0

        engine = create_async_engine(
            settings.database_url,
            pool_size=1,
            max_overflow=2,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    except Exception as exc:
        _log.error("Cron scheduler failed to create engine: %s", exc)
        return

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        while True:
            try:
                if stop_event.is_set():
                    break
                async with factory() as session, session.begin():
                    await set_rls_org(session, uuid.UUID("00000000-0000-0000-0000-000000000000"))
                    now = datetime.now(UTC)
                    result = await session.execute(
                        select(Trigger).where(
                            Trigger.trigger_type == "cron",
                            Trigger.active.is_(True),
                            or_(Trigger.next_fire_at.is_(None), Trigger.next_fire_at <= now),
                        )
                    )
                    for trigger in result.scalars():
                        result_dict = await fire_cron_trigger(
                            trigger_id=trigger.id,
                            org_id=trigger.organisation_id,
                            pipeline_id=trigger.pipeline_id,
                            cron_expression=trigger.cron_expression or "",
                            snapshot_id=trigger.config_json.get("snapshot_id") if trigger.config_json else None,
                            factory=factory,
                        )
                        if result_dict.get("status") == "fired" and result_dict.get("run_id"):
                            run_id_str = result_dict["run_id"]
                            if bg_worker is not None:
                                try:
                                    bg_worker.submit(
                                        run_id=uuid.UUID(run_id_str),
                                        org_id=trigger.organisation_id,
                                        input_payload=result_dict.get("input_payload", {}),
                                    )
                                except Exception:
                                    _log.exception(
                                        "Failed to submit cron-triggered run %s to background worker",
                                        run_id_str,
                                    )
                            else:
                                try:
                                    await dispatch_run(
                                        run_id_str,
                                        str(trigger.organisation_id),
                                        queue="runs",
                                        celery_queue="runs_automated",
                                    )
                                except Exception:
                                    _log.exception(
                                        "Failed to dispatch cron-triggered run %s to Celery",
                                        run_id_str,
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
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                _log.info("Cron scheduler cancelled during sleep")
                break
    except Exception as exc:
        _log.error("Cron scheduler unexpected error: %s", exc)
    finally:
        await engine.dispose()
        _log.info("Cron scheduler stopped")


async def run_scheduler(
    stop_event: asyncio.Event,
    bg_worker: Any | None = None,
) -> None:
    """Poll for due cron triggers and fire them, submitting created runs to the background worker.

    Auto-restarts the inner loop on unexpected exit so a transient crash
    (e.g. DB connection loss, CancelledError during sleep) does not cause
    a permanent outage.
    """
    restart_delay = 1.0
    max_delay = 30.0
    while True:
        try:
            await _run_scheduler_loop(stop_event, bg_worker)
            # Clean exit via stop_event — do not restart.
            return
        except asyncio.CancelledError:
            _log.info("Cron scheduler cancelled at top level — stopping")
            return
        except BaseException:
            _log.exception("Cron scheduler crashed — restarting in %.1fs", restart_delay)
            with suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.sleep(restart_delay),
                    timeout=restart_delay + 1.0,
                )
            restart_delay = min(restart_delay * 2, max_delay)
