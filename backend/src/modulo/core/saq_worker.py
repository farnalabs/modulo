"""SAQ worker settings + custom system web runner.

Two worker processes (plan F1/F2):

* ``runs_settings`` — queue ``runs``, concurrency 5 (derived from SAQ_REDIS_POOL_SIZE), no web UI. Executes
  ``execute_run``/``resume_run`` jobs and the per-item fire jobs
  (``fire_cron_trigger``/``fire_polling_trigger``/``fire_report_trigger``).
* ``system_settings`` — queue ``system``, concurrency 5 (derived from SAQ_REDIS_POOL_SIZE), web UI on 8081 bound
  to 127.0.0.1 (``fly ssh`` only), FAIL-CLOSED auth: refuses to boot unless
  ``SAQ_AUTH_PASSWORD`` and ``SAQ_AUTH_USERNAME`` are set. Owns the system
  crons: fire_due_triggers, dispatcher_reconcile, claim-expiry, retention,
  webhook-dedup cleanup, stale_run_recovery.

Staging uses the SAME workers on dedicated queue names so a staging worker can
never dequeue production system jobs: ``staging_runs_settings`` (queue
``staging-runs``) and ``staging_system_settings`` (queue ``staging-system``).
Staging configures the queue names via ``SAQ_RUNS_QUEUE=staging-runs``; the
worker queue names ALWAYS derive from ``settings.saq_runs_queue`` so workers,
``dispatch_run``, ``fire_due_triggers``, and the health gate stay in sync.

SAQ 0.26.4 CLI invocation (no ``worker`` subcommand — the settings path is the
only positional arg)::

    python -m saq core.saq_worker.runs_settings

The ``runs`` worker has no web UI and uses the plain CLI. The ``system`` worker
MUST NOT use ``python -m saq core.saq_worker.system_settings --web`` — the plain
``--web`` CLI binds 0.0.0.0 (aiohttp ``run_app`` has no ``host`` flag) and does
NOT set the ``AUTH_PASSWORD``/``AUTH_USER`` env vars that ``saq/web/aiohttp.py``
requires for BasicAuth. The system worker therefore ships a CUSTOM RUNNER
(:func:`run_system_web`) that runs the worker (queue=system, crons + functions)
and the web app in the same process, calling ``aiohttp.web.run_app(host=
"127.0.0.1")`` and mapping ``SAQ_AUTH_USERNAME``/``SAQ_AUTH_PASSWORD`` to the
``AUTH_USER``/``AUTH_PASSWORD`` env vars SAQ's web reads. Run it instead::

    python -m modulo.core.saq_worker
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import uuid
from typing import Any

from redis import asyncio as aioredis
from saq import CronJob, Worker
from saq.queue.redis import RedisQueue
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# Shared worker lifecycle knobs (plan F2).
# SAQ runs asyncio jobs in a single process sharing one engine, so raising
# concurrency does NOT multiply DB connection pools the way Celery prefork
# does. Sandbox-agent runs spend most of their time awaiting external E2B
# sandboxes; concurrency derives from SAQ_REDIS_POOL_SIZE (default 5).
# Pool and concurrency move together — one knob.
_SHUTDOWN_GRACE_PERIOD_S = 30
_CANCELLATION_HARD_DEADLINE_S = 60
_DEQUEUE_TIMEOUT = 5
# worker_info:89 -> TTL 90 (timer+1 is ALWAYS the TTL in saq 0.26.4).
_TIMERS: dict[str, float] = {"schedule": 5, "worker_info": 89, "sweep": 60, "abort": 1}

# Web UI bind (F8): fly ssh only.
_SYSTEM_WEB_HOST = "127.0.0.1"
_SYSTEM_WEB_PORT = 8081

# Engine for run execution (SAQ path) — per-worker DB pool (plan F4).
_ASYNC_ENGINE: AsyncEngine | None = None


def _get_async_engine() -> AsyncEngine:
    global _ASYNC_ENGINE
    if _ASYNC_ENGINE is None:
        settings = get_settings()
        kw: dict[str, Any] = {"url": settings.database_url}
        if settings.modulo_db.lower() == "postgres":
            kw["connect_args"] = {"timeout": 10, "ssl": False}
            kw["pool_size"] = settings.saq_worker_db_pool_size
            kw["max_overflow"] = 0
        _ASYNC_ENGINE = create_async_engine(**kw)
    return _ASYNC_ENGINE


def _build_queue(queue_name: str) -> RedisQueue:
    """Build an SAQ RedisQueue with the Upstash-pinned client knobs (F2)."""
    settings = get_settings()
    redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
        max_connections=settings.saq_redis_pool_size,
    )
    return RedisQueue(redis_client, name=queue_name)


# ---------------------------------------------------------------------------
# Job functions — execute / resume (plan F4 / F6a)
# ---------------------------------------------------------------------------


async def execute_run(ctx: dict[str, Any], *, run_id: str, org_id: str) -> dict[str, Any]:
    """SAQ ``execute_run`` job — claim + execute + complete (SAQ claim window)."""
    from modulo.core.pipeline_execution import (
        claim_run_async,
        heartbeat_loop,
        load_and_setup,
        mark_complete,
    )

    aeng = _get_async_engine()
    rid = uuid.UUID(run_id)
    oid = uuid.UUID(org_id)
    job = ctx.get("job")

    claimed = await claim_run_async(aeng, run_id, org_id)
    if not claimed:
        _log.warning("SAQ execute_run: run %s not claimed (already handled or wrong state)", rid)
        return {"status": "not_claimed"}

    run, executor = await load_and_setup(aeng, rid, oid)
    if run is None:
        return {"status": "missing"}

    heartbeat_task: asyncio.Task[Any] | None = None
    try:
        heartbeat_task = asyncio.create_task(  # nosemgrep: create-task-without-guard
            heartbeat_loop(aeng, run_id, org_id, job=job),
            name=f"saq-heartbeat-{rid}",
        )
        await executor.execute(run_id=rid, org_id=oid, input_payload=run.input_payload or {})
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("SAQ execute_run failed for run %s", rid)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    await mark_complete(aeng, run_id, org_id)
    return {"status": "complete"}


async def resume_run(
    ctx: dict[str, Any],
    *,
    run_id: str,
    org_id: str,
    resume_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SAQ ``resume_run`` job — claim (awaiting_human/claimed or stale-running) + resume."""
    from modulo.core.pipeline_execution import resume_run as resume_run_core

    aeng = _get_async_engine()
    return await resume_run_core(
        async_engine=aeng,
        run_id=run_id,
        org_id=org_id,
        resume_data=resume_data,
        job=ctx.get("job"),
    )


# ---------------------------------------------------------------------------
# Job functions — per-item fire jobs (plan F1)
# ---------------------------------------------------------------------------


async def fire_cron_trigger(
    ctx: dict[str, Any],
    *,
    trigger_id: str,
    org_id: str,
    pipeline_id: str,
    cron_expression: str,
    snapshot_id: str = "",
) -> dict[str, Any]:
    """Per-item cron fire job — fire + dispatch the created run (SAQ)."""
    from modulo.core import cron_helpers as _ch
    from modulo.core.dispatch import dispatch_run

    result = await _ch.fire_cron_trigger(
        trigger_id=uuid.UUID(trigger_id),
        org_id=uuid.UUID(org_id),
        pipeline_id=uuid.UUID(pipeline_id),
        cron_expression=cron_expression,
        snapshot_id=uuid.UUID(snapshot_id) if snapshot_id else None,
    )
    if result.get("status") == "fired" and result.get("run_id"):
        try:
            outcome, job_id = await dispatch_run(result["run_id"], org_id, queue=get_settings().saq_runs_queue)
            result["dispatch"] = outcome
            result["job_id"] = job_id
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("fire_cron_trigger: dispatch failed for run %s", result["run_id"])
    return result


async def fire_polling_trigger(
    ctx: dict[str, Any],
    *,
    trigger_id: str,
    org_id: str,
    pipeline_id: str,
    connector_instance_id: str,
    poll_query: str,
    condition_expression: str | None = None,
) -> dict[str, Any]:
    """Per-item polling fire job — fire + dispatch the created run (SAQ)."""
    from modulo.core import cron_helpers as _ch
    from modulo.core.dispatch import dispatch_run

    result = await _ch.fire_polling_trigger(
        trigger_id=uuid.UUID(trigger_id),
        org_id=uuid.UUID(org_id),
        pipeline_id=uuid.UUID(pipeline_id),
        connector_instance_id=uuid.UUID(connector_instance_id),
        poll_query=poll_query,
        condition_expression=condition_expression,
    )
    if result.get("status") == "fired" and result.get("run_id"):
        try:
            outcome, job_id = await dispatch_run(result["run_id"], org_id, queue=get_settings().saq_runs_queue)
            result["dispatch"] = outcome
            result["job_id"] = job_id
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("fire_polling_trigger: dispatch failed for run %s", result["run_id"])
    return result


async def fire_report_trigger(ctx: dict[str, Any], *, report_id: str, org_id: str) -> dict[str, Any]:
    """Per-item report fire job — generate + deliver (SAQ bounded job)."""
    from modulo.core import cron_helpers as _ch

    return await _ch.fire_report_trigger(report_id=uuid.UUID(report_id), org_id=uuid.UUID(org_id))


# ---------------------------------------------------------------------------
# Job functions — system worker (plan F1 / F3c / PR B step 6)
# ---------------------------------------------------------------------------


async def fire_due_triggers(ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — read due rows, atomic next_fire_at advance, enqueue fire jobs."""
    from modulo.core import cron_helpers as _ch

    return await _ch.fire_due_triggers()


async def dispatcher_reconcile(ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — re-dispatch runs whose SAQ job is missing (every 60s)."""
    from modulo.core import cron_helpers as _ch

    return await _ch.dispatcher_reconcile()


async def claim_expiry(ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — expire stale HITL claims (SAQ SOLE writer/notifier, F1)."""
    from modulo.core.hitl_manager.expiry_job import expire_stale_claims
    from modulo.core.notifier import Notifier

    settings = get_settings()
    factory = _make_session_factory()
    notifier: Notifier | None = None
    try:
        notifier = Notifier(_get_async_engine(), settings.fernet_key)
    except Exception:
        _log.exception("claim_expiry: notifier init failed — DB expiry still runs")
    expired = await expire_stale_claims(factory, notifier=notifier)
    return {"expired": len(expired)}


async def retention_cleanup(ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — batch-delete terminal runs older than the retention window."""
    from modulo.db.crud.run import batch_delete_old_terminal_runs

    async with _make_session_factory()() as session, session.begin():
        deleted = await batch_delete_old_terminal_runs(session)
    if deleted:
        _log.info("saq.retention_cleanup.deleted_old_runs", extra={"count": deleted})
    return {"deleted": deleted}


async def webhook_dedup_cleanup(ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — purge old webhook trigger events (30-day retention)."""
    from modulo.core.cleanup_jobs.webhook_dedup_cleanup import BATCH_SIZE, cleanup_old_webhook_events

    total = 0
    async with _make_session_factory()() as session:
        while True:
            deleted = await cleanup_old_webhook_events(session)
            total += deleted
            if deleted < BATCH_SIZE:
                break
    return {"deleted": total}


async def stale_run_recovery(ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — legacy stale-run sweep, scoped to non-SAQ rows (F1)."""
    from modulo.core.pipeline_execution import stale_run_recovery_sweep

    return await stale_run_recovery_sweep(_get_async_engine())


# ---------------------------------------------------------------------------
# Worker settings
# ---------------------------------------------------------------------------

_HOSTNAME = os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or "unknown"


def _runs_queue_name() -> str:
    """Runs worker queue — derives from ``SAQ_RUNS_QUEUE`` (settings).

    ``dispatch_run``, ``fire_due_triggers``, and the health gate all enqueue/
    check this exact queue name; the worker MUST listen on the same one or jobs
    are enqueued but never dequeued (plan F3 / review).
    """
    return get_settings().saq_runs_queue


def _system_queue_name() -> str:
    """System worker queue — derived from the runs queue.

    Matches ``health._configured_queues`` (``runs_queue.replace("runs",
    "system")``) so the readiness gate checks the queues the workers actually
    listen on. Falls back to ``"system"`` for a runs queue name without
    ``"runs"``.
    """
    runs_queue = get_settings().saq_runs_queue
    return runs_queue.replace("runs", "system") if "runs" in runs_queue else "system"


def _base_worker_settings(queue_name: str, functions: list[Any]) -> dict[str, Any]:
    return {
        "queue": _build_queue(queue_name),
        "functions": functions,
        "concurrency": get_settings().saq_redis_pool_size,
        "shutdown_grace_period_s": _SHUTDOWN_GRACE_PERIOD_S,
        "cancellation_hard_deadline_s": _CANCELLATION_HARD_DEADLINE_S,
        "dequeue_timeout": _DEQUEUE_TIMEOUT,
        "timers": dict(_TIMERS),
        "after_process": _after_process_hook,
        # Machine-scoped worker metadata for /healthz/ready (plan F7).
        "metadata": {"hostname": _HOSTNAME},
    }


async def _after_process_hook(ctx: dict[str, Any]) -> None:
    from modulo.core.error_tracking.saq_hooks import after_process

    await after_process(ctx)


def _make_session_factory() -> Any:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(_get_async_engine(), expire_on_commit=False, autobegin=False)


def _runs_functions() -> list[tuple[str, Any]]:
    """Functions registered on the ``runs`` worker.

    Names match the strings enqueued by dispatch_run and fire_due_triggers.
    """
    return [
        ("modulo.core.saq_worker.execute_run", execute_run),
        ("modulo.core.saq_worker.resume_run", resume_run),
        ("modulo.core.saq_worker.fire_cron_trigger", fire_cron_trigger),
        ("modulo.core.saq_worker.fire_polling_trigger", fire_polling_trigger),
        ("modulo.core.saq_worker.fire_report_trigger", fire_report_trigger),
    ]


def _system_functions() -> list[Any]:
    """Functions registered on the ``system`` worker (under their ``__qualname__``,
    which is the name SAQ's cron scheduler uses when enqueueing)."""
    return [
        fire_due_triggers,
        dispatcher_reconcile,
        claim_expiry,
        retention_cleanup,
        webhook_dedup_cleanup,
        stale_run_recovery,
    ]


def _system_cron_jobs() -> list[CronJob[Any]]:
    """System cron jobs (plan F1) — all knobs explicit."""
    return [
        # fire_due_triggers: every 60s (croniter parses 5-field cron, so the
        # 30s intent is not achievable — every minute); the atomic next_fire_at
        # advance makes multi-machine ticks safe (unique=True only prevents overlap).
        CronJob(
            fire_due_triggers,
            cron="* * * * *",
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=3,
            ttl=300,
        ),
        # dispatcher_reconcile: every 60s (timeout=120 per plan F1).
        CronJob(
            dispatcher_reconcile,
            cron="* * * * *",
            unique=True,
            timeout=120,
            heartbeat=30,
            retries=3,
            ttl=300,
        ),
        # claim-expiry: every 60s — SAQ cron is the SOLE writer/notifier (F1).
        CronJob(
            claim_expiry,
            cron="* * * * *",
            unique=True,
            timeout=120,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # retention: hourly (matches the in-process _run_retention_loop cadence).
        CronJob(
            retention_cleanup,
            cron="0 * * * *",
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # webhook-dedup cleanup: hourly (matches _CLEANUP_INTERVAL_SECONDS).
        CronJob(
            webhook_dedup_cleanup,
            cron="0 * * * *",
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # stale_run_recovery: every 5 min (legacy beat cadence, scoped to
        # non-SAQ rows in the sweep itself).
        CronJob(
            stale_run_recovery,
            cron="*/5 * * * *",
            unique=True,
            timeout=120,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
    ]


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
    return _base_worker_settings(_runs_queue_name(), _runs_functions())


def system_settings() -> dict[str, Any]:
    """WorkerSettings for the ``system`` worker (web UI, FAIL-CLOSED auth, crons)."""
    _assert_system_auth_configured()
    return {**_base_worker_settings(_system_queue_name(), _system_functions()), "cron_jobs": _system_cron_jobs()}


def staging_runs_settings() -> dict[str, Any]:
    """Staging ``runs`` worker — queue derives from ``SAQ_RUNS_QUEUE=staging-runs``."""
    return _base_worker_settings(_runs_queue_name(), _runs_functions())


def staging_system_settings() -> dict[str, Any]:
    """Staging ``system`` worker — queue derives from the staging runs queue."""
    _assert_system_auth_configured()
    return {**_base_worker_settings(_system_queue_name(), _system_functions()), "cron_jobs": _system_cron_jobs()}


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
