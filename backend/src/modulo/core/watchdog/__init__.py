"""In-process watchdog tasks for platform-level liveness alerting.

Watchdog tasks run as asyncio background tasks in the FastAPI lifespan,
NOT as SAQ cron jobs. This ensures alerts work even when SAQ workers are down.

Usage::

    from modulo.core.watchdog import run_worker_liveness_watchdog

    # Started by the FastAPI lifespan
    asyncio.create_task(run_worker_liveness_watchdog())
"""

from modulo.core.watchdog.worker_liveness import (
    run_worker_liveness_watchdog,
    WATCHDOG_HEARTBEAT_TTL_SECONDS,
)

__all__ = [
    "run_worker_liveness_watchdog",
    "WATCHDOG_HEARTBEAT_TTL_SECONDS",
]
