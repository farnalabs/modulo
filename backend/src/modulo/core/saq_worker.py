"""SAQ worker settings + custom system web runner.

Two worker processes (plan F1/F2):

* ``runs_settings`` — queue ``runs``, concurrency 10, no web UI. Executes
  ``execute_run``/``resume_run`` jobs (wired in PR B).
* ``system_settings`` — queue ``system``, concurrency 10, web UI on 8081 bound
  to 127.0.0.1 (``fly ssh`` only), FAIL-CLOSED auth: refuses to boot unless
  ``SAQ_AUTH_PASSWORD`` and ``SAQ_AUTH_USERNAME`` are set.

Staging uses the SAME workers on dedicated queue names so a staging worker can
never dequeue production system jobs: ``staging_runs_settings`` (queue
``staging-runs``) and ``staging_system_settings`` (queue ``staging-system``).

SAQ 0.26.4 CLI invocation (no ``worker`` subcommand — the settings path is the
only positional arg)::

    python -m saq core.saq_worker.runs_settings
    python -m saq core.saq_worker.system_settings --web --port 8081

The plain ``--web`` CLI binds 0.0.0.0 (aiohttp ``run_app`` has no ``host``
flag). The system worker therefore ships a CUSTOM RUNNER
(:func:`run_system_web`) that calls ``aiohttp.web.run_app(host="127.0.0.1")``
and maps ``SAQ_AUTH_USERNAME`` to the ``AUTH_USER`` env var SAQ's web reads
(``saq/web/aiohttp.py``). Run it instead of the CLI::

    python -m modulo.core.saq_worker
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from redis import asyncio as aioredis
from saq import Worker
from saq.queue.redis import RedisQueue

from modulo.settings import get_settings

# Shared worker lifecycle knobs (plan F2).
# SAQ runs asyncio jobs in a single process sharing one engine, so raising
# concurrency does NOT multiply DB connection pools the way Celery prefork
# does. Sandbox-agent runs spend most of their time awaiting external E2B
# sandboxes; 10 concurrent jobs is cheap. Bumped from 2 on 2026-08-01.
_WORKER_CONCURRENCY = 10
_SHUTDOWN_GRACE_PERIOD_S = 30
_CANCELLATION_HARD_DEADLINE_S = 60
_DEQUEUE_TIMEOUT = 5
# worker_info:89 -> TTL 90 (timer+1 is ALWAYS the TTL in saq 0.26.4).
_TIMERS: dict[str, float] = {"schedule": 5, "worker_info": 89, "sweep": 60, "abort": 1}

# Web UI bind (F8): fly ssh only.
_SYSTEM_WEB_HOST = "127.0.0.1"
_SYSTEM_WEB_PORT = 8081


def _build_queue(queue_name: str) -> RedisQueue:
    """Build an SAQ RedisQueue with the Upstash-pinned client knobs (F2)."""
    settings = get_settings()
    redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url,
        connection_timeout=10,
        socket_keepalive=True,
        max_connections=settings.saq_redis_pool_size,
    )
    return RedisQueue(redis=redis_client, name=queue_name)


def _base_worker_settings(queue_name: str) -> dict[str, Any]:
    return {
        "queue": _build_queue(queue_name),
        # SAQ job functions (execute_run, resume_run, fire_*, report,
        # dispatcher_reconcile) are wired in PR B. An empty list keeps the
        # Worker constructible during PR A.
        "functions": [],
        "concurrency": _WORKER_CONCURRENCY,
        "shutdown_grace_period_s": _SHUTDOWN_GRACE_PERIOD_S,
        "cancellation_hard_deadline_s": _CANCELLATION_HARD_DEADLINE_S,
        "dequeue_timeout": _DEQUEUE_TIMEOUT,
        "timers": dict(_TIMERS),
    }


def _assert_system_auth_configured() -> None:
    """Fail-closed: the system worker refuses to boot without web auth (F1)."""
    settings = get_settings()
    if not settings.saq_auth_password:
        raise RuntimeError(
            "Refusing to boot SAQ system worker: SAQ_AUTH_PASSWORD must be set (fail-closed web UI auth)."
        )
    if not settings.saq_auth_username:
        raise RuntimeError(
            "Refusing to boot SAQ system worker: SAQ_AUTH_USERNAME must be set (fail-closed web UI auth)."
        )


def runs_settings() -> dict[str, Any]:
    """WorkerSettings for the ``runs`` worker (no web UI)."""
    return _base_worker_settings("runs")


def system_settings() -> dict[str, Any]:
    """WorkerSettings for the ``system`` worker (web UI, FAIL-CLOSED auth).

    ``cron_jobs`` is an empty list for now — the crons (fire_due_triggers,
    dispatcher_reconcile, retention, claim-expiry) arrive in PR B. The
    structure is declared here so the Worker accepts the key and PR B can fill
    it without changing the settings shape.
    """
    _assert_system_auth_configured()
    return {**_base_worker_settings("system"), "cron_jobs": []}


def staging_runs_settings() -> dict[str, Any]:
    """Staging ``runs`` worker — dedicated queue ``staging-runs``."""
    return _base_worker_settings("staging-runs")


def staging_system_settings() -> dict[str, Any]:
    """Staging ``system`` worker — dedicated queue ``staging-system``."""
    _assert_system_auth_configured()
    return {**_base_worker_settings("staging-system"), "cron_jobs": []}


def run_system_web() -> None:
    """Run the SAQ system worker + web UI bound to 127.0.0.1 (custom runner).

    aiohttp ``run_app`` has no ``host`` flag and defaults to 0.0.0.0; this
    runner passes ``host="127.0.0.1"`` so the web UI is only reachable via
    ``fly ssh`` (plan F8). SAQ's web reads ``AUTH_PASSWORD`` / ``AUTH_USER``
    from the environment (``saq/web/aiohttp.py``) — map the settings values
    there. Auth is fail-closed: boot raises if either value is unset.
    """
    from aiohttp import web
    from saq.web.aiohttp import create_app

    _assert_system_auth_configured()
    settings = get_settings()
    os.environ["AUTH_PASSWORD"] = settings.saq_auth_password or ""
    os.environ["AUTH_USER"] = settings.saq_auth_username or "admin"

    worker = Worker(**system_settings())
    loop = asyncio.new_event_loop()

    async def _worker_start() -> None:
        try:
            await worker.queue.connect()
            await worker.start()
        finally:
            await worker.queue.disconnect()

    async def _shutdown(_app: Any) -> None:
        await worker.stop()

    queue = worker.queue
    app = create_app([queue])
    app.on_shutdown.append(_shutdown)

    loop.create_task(_worker_start()).add_done_callback(lambda _: signal.raise_signal(signal.SIGTERM))
    web.run_app(app, host=_SYSTEM_WEB_HOST, port=_SYSTEM_WEB_PORT, loop=loop)


def main() -> None:
    """Entry point for ``python -m modulo.core.saq_worker`` (system worker)."""
    run_system_web()


if __name__ == "__main__":
    main()
