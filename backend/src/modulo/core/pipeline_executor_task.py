"""Celery task that sweeps stale pending/running pipeline runs.

Fired every 5 minutes by ``StaleRecoveryEntry`` in the ``CompositeScheduler``.
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

try:
    from celery import Task
except ImportError:
    import typing

    if typing.TYPE_CHECKING:
        from celery import Task
    Task = object

_log = logging.getLogger(__name__)


class StaleRunRecoveryTask(Task):  # type: ignore[misc]
    """Celery task that sweeps stale pending and running pipeline runs."""

    name = "modulo.pipeline.stale_run_recovery"
    autoretry_for = (Exception,)
    max_retries = 2
    default_retry_delay = 300

    def run(self) -> dict[str, Any]:
        import asyncio

        return asyncio.run(_stale_run_recovery_sweep())


async def _stale_run_recovery_sweep() -> dict[str, Any]:
    """Sweep stale pending and running pipeline runs.

    - Pending runs older than 5 minutes with no ``dispatched_at`` are marked
      ``failed`` with ``never_dispatched``.
    - Running runs with a heartbeat older than 10 minutes and 5+ claims are
      marked ``failed`` with ``worker_lost``.
    """
    from modulo.settings import get_settings

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_size=1,
        max_overflow=2,
        pool_pre_ping=True,
        pool_timeout=10,
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            never_result = await session.execute(
                text("""
                    UPDATE runs
                    SET status = 'failed',
                        error_code = 'never_dispatched',
                        completed_at = now()
                    WHERE status = 'pending'
                      AND created_at < now() - interval '5 minutes'
                      AND dispatched_at IS NULL
                """)
            )
            never_count = never_result.rowcount

            lost_result = await session.execute(
                text("""
                    UPDATE runs
                    SET status = 'failed',
                        error_code = 'worker_lost',
                        completed_at = now()
                    WHERE status = 'running'
                      AND heartbeat_at < now() - interval '10 minutes'
                      AND claim_count >= 5
                """)
            )
            lost_count = lost_result.rowcount

        if never_count or lost_count:
            _log.info(
                "Stale run recovery: %d never-dispatched, %d worker-lost runs swept",
                never_count,
                lost_count,
            )
        return {
            "never_dispatched_swept": never_count,
            "worker_lost_swept": lost_count,
        }
    except Exception:
        _log.exception("Stale run recovery sweep failed")
        return {
            "never_dispatched_swept": 0,
            "worker_lost_swept": 0,
            "error": "sweep_failed",
        }
    finally:
        await engine.dispose()
