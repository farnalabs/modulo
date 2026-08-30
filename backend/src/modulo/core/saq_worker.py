"""SAQ worker settings + custom system web runner.

Two worker processes (plan F1/F2):

* ``runs_settings`` — queue ``runs``, concurrency (SAQ_WORKER_CONCURRENCY,
  default 5, deployed at 20 in prod/staging; the Redis pool must stay strictly
  larger — see :func:`_effective_redis_pool_size`), no web UI. Executes
  ``execute_run``/``resume_run`` jobs and the per-item fire jobs
  (``fire_cron_trigger``/``fire_polling_trigger``/``fire_report_trigger``/
  ``fire_ongoing_trigger``).
* ``system_settings`` — queue ``system``, concurrency (SAQ_WORKER_CONCURRENCY,
  default 5, deployed at 20 in prod/staging; the Redis pool must stay strictly
  larger — see :func:`_effective_redis_pool_size`), web UI on 8081 bound
  to 127.0.0.1 (``fly ssh`` only), FAIL-CLOSED auth: refuses to boot unless
  ``SAQ_AUTH_PASSWORD`` and ``SAQ_AUTH_USERNAME`` are set. Owns the system
  crons: fire_due_triggers, dispatcher_reconcile, claim-expiry, retention,
  webhook-dedup cleanup, stale_run_recovery.

Accepted design target: concurrency 20 per worker x up to 5 machines = up to 100
concurrent runs (recorded in ADR 017).

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
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from redis import asyncio as aioredis
from saq import CronJob, Worker
from saq.queue.redis import RedisQueue
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# Shared worker lifecycle knobs (plan F2).
# SAQ runs asyncio jobs in a single process sharing one engine, so raising
# concurrency does NOT multiply DB connection pools the way Celery prefork
# does. Sandbox-agent runs spend most of their time awaiting external E2B
# sandboxes; concurrency is controlled by SAQ_WORKER_CONCURRENCY (default 5)
# and decoupled from the Redis pool size.
_SHUTDOWN_GRACE_PERIOD_S = 30
_CANCELLATION_HARD_DEADLINE_S = 60
_DEQUEUE_TIMEOUT = 5
# worker_info:89 -> TTL 90 (timer+1 is ALWAYS the TTL in saq 0.26.4).
_TIMERS: dict[str, float] = {"schedule": 5, "worker_info": 89, "sweep": 60, "abort": 1}

# System-cron cadences (5-field form — croniter parses 5 fields, so the 30s
# intent is not achievable; every minute is the floor).
_CRON_EVERY_MINUTE = "* * * * *"
_CRON_EVERY_5_MINUTES = "*/5 * * * *"
_CRON_HOURLY = "0 * * * *"


def _sync_interval_to_cron(interval_seconds: int) -> str:
    """Map the community-library sync interval (seconds) to a 5-field cron.

    croniter parses 5-field cron with a 1-minute floor, so sub-minute intervals
    collapse to every minute. Multiples of 3600 map to an hourly cadence
    (``0 */N * * *``); anything else is expressed as ``*/N * * * *`` minutes.
    """
    if interval_seconds <= 60:
        return _CRON_EVERY_MINUTE
    minutes = max(1, interval_seconds // 60)
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours >= 24:
            return "0 0 * * *"
        return f"0 */{hours} * * *"
    return f"*/{minutes} * * * *"


# Web UI bind (F8): fly ssh only.
_SYSTEM_WEB_HOST = "127.0.0.1"
_SYSTEM_WEB_PORT = 8081

# Engine for run execution (SAQ path) — the shared per-process engine (D4).
# ``get_shared_engine`` builds ONE engine per process with the Fly/HAProxy
# knobs (pool_pre_ping, statement_cache_size=0); the per-worker pool budget
# (``saq_worker_db_pool_size``) is passed as the creation-time override. The
# local cache mirrors the historical pattern so tests can reset it per test.
_ASYNC_ENGINE: AsyncEngine | None = None


def _get_async_engine() -> AsyncEngine:
    global _ASYNC_ENGINE
    if _ASYNC_ENGINE is None:
        # Lazy import keeps db.session (and its module-level `engine`) out of
        # the worker's import graph until the first engine is actually needed.
        from modulo.db.session import get_shared_engine

        settings = get_settings()
        if settings.modulo_db.lower() == "postgres":
            effective_pool = _effective_db_pool_size(settings.saq_worker_db_pool_size, settings.saq_worker_concurrency)
            if effective_pool != settings.saq_worker_db_pool_size:
                _log.warning(
                    "saq_worker.db_pool_raised",
                    extra={
                        "configured_pool": settings.saq_worker_db_pool_size,
                        "effective_pool": effective_pool,
                        "concurrency": settings.saq_worker_concurrency,
                    },
                )
            _ASYNC_ENGINE = get_shared_engine(
                pool_size=effective_pool,
                max_overflow=0,
            )
        else:
            _ASYNC_ENGINE = get_shared_engine()
    return _ASYNC_ENGINE


def _max_concurrent_ops(pool_size: int) -> int:
    """Clamp SAQ's ``max_concurrent_ops`` strictly below the Redis pool size.

    The semaphore must never exhaust all available connections — leaving
    reserve connections for SAQ operations that bypass the semaphore
    (``schedule``, ``sweep``, ``dequeue``, ``notify``). The old
    ``max(pool_size - 5, 5)`` clamp was broken: at pool_size 5 it allowed
    ``max_ops == pool_size`` (zero reserve) and for pool_size < 5 it allowed
    ``max_ops > pool_size`` — the exact 'Too many connections' exhaustion it
    claims to prevent.

    The correct clamp always leaves at least one reserve connection: small
    pools (2-5) reserve 1, larger pools keep the historical 5-connection
    margin. A pool of 1 gets the whole single connection.
    """
    if pool_size <= 1:
        return pool_size
    if pool_size <= 5:
        return pool_size - 1
    return pool_size - 5


def _effective_redis_pool_size(pool_size: int, concurrency: int) -> int:
    """Guarantee the SAQ Redis pool is large enough for blocking dequeue.

    SAQ's ``dequeue()`` uses a blocking ``blmove`` (``_DEQUEUE_TIMEOUT``) that
    is NOT gated by ``max_concurrent_ops`` — every concurrent ``_process``
    task holds one pool connection while blocked. When the queue drains, all
    ``concurrency`` connections can be held simultaneously, leaving nothing
    for the Upkeep tasks (``schedule``/``sweep``/``abort``/``worker_info``),
    which raises ``ConnectionError: Too many connections`` and kills the
    worker's heartbeats (the silent wedge, 2026-08-10).

    Enforce ``pool >= concurrency + reserve`` where reserve covers the upkeep
    ops (a minimum of 5, matching the historical non-dequeue margin).
    """
    reserve = 5
    return max(pool_size, concurrency + reserve)


# Estimated DB connections drawn per in-flight run. A single run is not one
# connection: ``load_and_setup`` opens its own session, the executor holds a
# connection through ``execute``/``resume``, and the heartbeat/watchdog
# terminalize path (``fail_run_terminal``) needs a slot to fail a run that
# wedges in setup. Sizing the floor for this fan-out (rather than assuming 1
# connection/run) is what prevents the pool from being exhausted in pre-node
# setup — the agent.stall root cause.
CONNS_PER_RUN = 3


def _effective_db_pool_size(pool_size: int, concurrency: int) -> int:
    """Guarantee the worker async DB pool is large enough for concurrent runs.

    Each concurrent run draws multiple DB connections (see :data:`CONNS_PER_RUN`),
    and the zombie watchdog's terminalize write (``fail_run_terminal``) needs a
    connection to fail a run that wedges in pre-node setup. With
    ``SAQ_WORKER_CONCURRENCY=20`` and 1 connection/run assumed (as the old
    ``concurrency + 5`` floor did), a 30-conn pool is exhausted (20 runs Ã— 2-3
    conns = 40-60 > 30) and ``load_and_setup`` wedges awaiting a connection —
    the run rides to the 35-min ``dispatcher_reconcile`` backstop as a nodeless
    zombie (the agent.stall symptom).

    Mirror :func:`_effective_redis_pool_size`: enforce
    ``pool >= concurrency * CONNS_PER_RUN + reserve`` so the floor covers the
    per-run connection fan-out plus a reserve for the watchdog terminalize
    writes and any system-cron connections sharing the same engine.
    """
    reserve = 5
    floor = concurrency * CONNS_PER_RUN + reserve
    return max(pool_size, floor)


def _build_queue(queue_name: str) -> RedisQueue:
    """Build an SAQ RedisQueue with the Upstash-pinned client knobs (F2).

    ``max_concurrent_ops`` is set below the pool size so the semaphore never
    exhausts all available connections — leaving reserve connections for SAQ
    operations that bypass the semaphore (``schedule``, ``sweep``, ``dequeue``,
    ``notify``).

    The client pool uses the EFFECTIVE pool size (never below
    ``concurrency + 5``) because SAQ's blocking ``dequeue()`` holds one pool
    connection per concurrent ``_process`` task regardless of
    ``max_concurrent_ops``; a pool smaller than the concurrency leaves nothing
    for the Upkeep tasks and silently wedges the worker.
    """
    settings = get_settings()
    pool_size = _effective_redis_pool_size(settings.saq_redis_pool_size, settings.saq_worker_concurrency)
    max_ops = _max_concurrent_ops(pool_size)
    if pool_size != settings.saq_redis_pool_size:
        _log.warning(
            "saq_worker.pool_raised",
            extra={
                "queue": queue_name,
                "configured_pool": settings.saq_redis_pool_size,
                "effective_pool": pool_size,
                "concurrency": settings.saq_worker_concurrency,
            },
        )
    redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
        max_connections=pool_size,
    )
    _check_redis_connection(redis_client)
    return RedisQueue(redis_client, name=queue_name, max_concurrent_ops=max_ops)


def _check_redis_connection(_redis_client: aioredis.Redis, max_retries: int = 3) -> None:
    """Validate Redis connectivity on worker startup with exponential backoff.

    Synchronous — called from the settings factory before the event loop is
    available. The async ``redis_client`` handed to SAQ is NOT pinged here:
    ``redis.asyncio`` pools are loop-affine, so awaiting its ``ping()`` in a
    throwaway probe loop would bind the pool to a discarded loop and break the
    worker's own loop. Instead a separate SYNC client (``socket_connect_timeout``)
    performs the probe, so the async client stays loop-neutral for SAQ. Logs a
    warning and retries up to ``max_retries`` times with exponential backoff
    instead of immediately crashing.
    """
    import redis as sync_redis

    settings = get_settings()
    sync_client = sync_redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5)
    try:
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                sync_client.ping()
                _log.info("Redis connection validated (attempt %d/%d)", attempt, max_retries)
                return
            except (sync_redis.ConnectionError, sync_redis.TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = 2**attempt
                    _log.warning(
                        "Redis ping failed (attempt %d/%d): %s — retrying in %ds",
                        attempt,
                        max_retries,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
        _log.error(
            "Redis unreachable after %d attempts: %s — worker proceeding anyway",
            max_retries,
            last_exc,
        )
    finally:
        sync_client.close()


async def reconcile_cron_registrations(queue: RedisQueue, cron_jobs: Collection[CronJob[Any]]) -> None:
    """Self-heal stale unique-cron registrations on system-worker startup.

    SAQ's ``Worker.schedule()`` re-enqueues every cron each ``schedule`` tick, but
    the underlying Redis enqueue Lua script DEDUPS on the job key
    (``cron:<function_qualname>``): it only writes the registration if the key is
    NOT already in the ``incomplete`` zset. So a cron's scheduled time is locked at
    whatever it was when the worker FIRST registered it, and only advances after the
    job actually fires. A worker restart does NOT clear the persisted ``incomplete``
    registration — so if a cron expression is changed in code AFTER it was first
    registered (as happened to ``metrics_dump``: ``0 1 * * *`` -> ``*/10 * * * *``),
    the stale schedule silently survives restarts and the job never re-aligns.

    This runs ONCE on startup (before ``worker.start()``) and clears each
    configured UNIQUE cron's persisted Redis registration (job hash + ``incomplete``
    zset member + ``queued`` list member) so SAQ's ``schedule()`` loop re-enqueues
    it with the CURRENT cron expression within ~1s at its correct next occurrence.
    It is idempotent and fail-open: a Redis hiccup must NEVER block worker boot, so
    a cron that errors during reconciliation is logged and skipped.

    A cron that is CURRENTLY executing lives in ``active``, not ``incomplete`` — so
    removing its incomplete/queued entries is a harmless no-op and it re-registers on
    finish. ``active`` is intentionally untouched.
    """
    for cron_job in cron_jobs:
        if not getattr(cron_job, "unique", False):
            continue
        function = getattr(cron_job, "function", None)
        if function is None:
            continue
        key = f"cron:{function.__qualname__}"
        job_id = queue.job_id(key)
        incomplete = queue.namespace("incomplete")
        queued = queue.namespace("queued")
        try:
            async with queue.redis.pipeline() as pipe:
                pipe.delete(job_id)
                pipe.zrem(incomplete, job_id)
                pipe.lrem(queued, 0, job_id)
                await pipe.execute()
            _log.info("saq.reconcile_cron_registrations.cleared", extra={"key": key})
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "saq.reconcile_cron_registrations.failed",
                extra={"key": key},
            )


def _probe_database() -> None:
    """Run a lightweight DB probe (``SELECT 1``) on worker startup.

    Uses a synchronous SQLAlchemy connection with a short timeout. Non-fatal —
    the DB may recover before the first real job arrives. Logs a warning on
    failure. The sync URL maps to ``psycopg`` (v3, the installed driver) — NOT
    ``psycopg2``, which is not in the dependency tree.
    """
    from sqlalchemy import create_engine, text

    settings = get_settings()
    sync_url = str(settings.database_url).replace("+asyncpg", "+psycopg").replace("+aiomysql", "+pymysql")
    engine = None
    try:
        engine = create_engine(sync_url, connect_args={"connect_timeout": 5}, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _log.info("Database connection probe passed")
    except Exception as exc:
        _log.warning("Database probe failed (non-fatal): %s — DB may recover before first job", exc)
    finally:
        if engine is not None:
            engine.dispose()


# ---------------------------------------------------------------------------
# Job functions — execute / resume (plan F4 / F6a)
# ---------------------------------------------------------------------------


async def execute_run(
    ctx: dict[str, Any], *, run_id: str, org_id: str, claim_token: str | None = None
) -> dict[str, Any]:
    """SAQ ``execute_run`` job — claim + execute + complete (SAQ claim window).

    Zombie protection (2026-08-05): the heartbeat loop starts BEFORE
    ``executor.execute``, so a run hung in the pre-node setup window (checkpointer
    setup, graph compile, connector hub init, or a DB ``OperationalError``) would
    otherwise stay ``running`` forever with a fresh heartbeat. Two guards:
    1. ``load_and_setup`` failures are caught and the run is terminal-failed
       (``executor_setup_failed``) instead of being left running.
    2. ``run_executor_with_watchdog`` starts a zombie watchdog that fails the
       run (``executor_stalled``) if no node dispatches within the setup grace
       window, and cancels the hung executor.

    The ``claim_token`` kwarg is the stale token stamped into this job's kwargs
    by a previous attempt (PR #1003). SAQ retries re-invoke this function with
    ``**job.kwargs``, so the kwarg is accepted and intentionally IGNORED here —
    a fresh claim is taken inside via ``claim_run_async`` and the fresh token is
    re-stamped.
    """
    from modulo.core.pipeline_execution import (
        EXECUTOR_SETUP_FAILED_ERROR_CODE,
        claim_run_async,
        fail_run_terminal,
        load_and_setup,
        mark_complete,
        run_executor_with_watchdog,
    )

    aeng = _get_async_engine()
    rid = uuid.UUID(run_id)
    oid = uuid.UUID(org_id)
    job = ctx.get("job")

    claim_token = await claim_run_async(aeng, run_id, org_id)
    if not claim_token:
        _log.warning("SAQ execute_run: run %s not claimed (already handled or wrong state)", rid)
        return {"status": "not_claimed"}

    # Stamp the claim token into the job hash so the after_process task_failure
    # hook can fence its terminal write: a failed job must not mark the run
    # failed when a successor already re-claimed it (dist/runtime-core A1).
    if job is not None:
        try:
            job_kwargs = getattr(job, "kwargs", None)
            merged_kwargs = {**(job_kwargs if isinstance(job_kwargs, dict) else {}), "claim_token": claim_token}
            await job.update(kwargs=merged_kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("SAQ execute_run: job kwargs stamp failed for run %s", rid)

    settings = get_settings()
    try:
        run, executor = await asyncio.wait_for(
            load_and_setup(aeng, rid, oid),
            timeout=settings.saq_setup_grace_seconds,
        )
    except TimeoutError:
        _log.exception("SAQ execute_run: load_and_setup timed out for run %s", rid)
        await fail_run_terminal(
            aeng,
            run_id,
            org_id,
            error_code=EXECUTOR_SETUP_FAILED_ERROR_CODE,
            error_detail=(
                f"load_and_setup (pre-node setup) exceeded setup grace "
                f"({settings.saq_setup_grace_seconds}s) — likely DB connection "
                "exhaustion or a wedged worker; was riding to the 35-min "
                "agent.stall backstop"
            ),
        )
        return {"status": "setup_failed"}
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("SAQ execute_run: load_and_setup failed for run %s", rid)
        await fail_run_terminal(
            aeng,
            run_id,
            org_id,
            error_code=EXECUTOR_SETUP_FAILED_ERROR_CODE,
            error_detail="load_and_setup failed before any node could run",
        )
        return {"status": "setup_failed"}
    if run is None:
        return {"status": "missing"}

    outcome = await run_executor_with_watchdog(
        aeng,
        run_id=run_id,
        org_id=org_id,
        executor=executor,
        job=job,
        claim_token=claim_token,
        execute_fn=lambda: executor.execute(
            run_id=rid,
            org_id=oid,
            input_payload=run.input_payload or {},
            claim_token=claim_token,
        ),
    )

    # Honest outcomes (A2): ``mark_complete`` runs ONLY on a genuine
    # completion. After a failure/awaiting_human/supersession it is a no-op
    # (the run is already terminal), so this is purely a guard against a
    # silent wrong-success write.
    if outcome.get("status") == "complete":
        await mark_complete(aeng, run_id, org_id, claim_token=claim_token)
    return outcome


async def resume_run(
    ctx: dict[str, Any],
    *,
    run_id: str,
    org_id: str,
    resume_data: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """SAQ ``resume_run`` job — claim (awaiting_human/claimed or stale-running) + resume.

    The ``claim_token`` kwarg is the stale token stamped into this job's kwargs
    by a previous attempt (PR #1003). SAQ retries re-invoke this function with
    ``**job.kwargs``, so the kwarg must be accepted here — it is intentionally
    IGNORED (the core claim ``claim_resume_run_async`` generates its own fresh
    token). Popping it from ``kwargs`` keeps the SAQ re-invocation contract
    intact while avoiding an unused-parameter lint.
    """
    kwargs.pop("claim_token", None)
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


async def _dispatch_created_run(result: dict[str, Any], *, org_id: str, log_context: str) -> dict[str, Any]:
    """Dispatch a run the fire job just created, sharing the dispatch path.

    Called only when a fire helper reports successful creation with a run id;
    attaches the dispatch outcome and the enqueued job id to ``result``. The
    ``dispatch_run`` import stays lazy, matching the surrounding worker jobs.
    """
    from modulo.core.dispatch import dispatch_run

    if result.get("status") == "fired" and result.get("run_id"):
        try:
            outcome, job_id = await dispatch_run(result["run_id"], org_id, queue=get_settings().saq_runs_queue)
            result["dispatch"] = outcome
            result["job_id"] = job_id
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("%s: dispatch failed for run %s", log_context, result["run_id"])
    return result


async def fire_cron_trigger(
    _ctx: dict[str, Any],
    *,
    trigger_id: str,
    org_id: str,
    pipeline_id: str,
    cron_expression: str,
    snapshot_id: str = "",
) -> dict[str, Any]:
    """Per-item cron fire job — fire + dispatch the created run (SAQ)."""
    from modulo.core import cron_helpers as _ch

    result = await _ch.fire_cron_trigger(
        trigger_id=uuid.UUID(trigger_id),
        org_id=uuid.UUID(org_id),
        pipeline_id=uuid.UUID(pipeline_id),
        cron_expression=cron_expression,
        snapshot_id=uuid.UUID(snapshot_id) if snapshot_id else None,
    )
    return await _dispatch_created_run(result, org_id=org_id, log_context="fire_cron_trigger")


async def fire_polling_trigger(
    _ctx: dict[str, Any],
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

    result = await _ch.fire_polling_trigger(
        trigger_id=uuid.UUID(trigger_id),
        org_id=uuid.UUID(org_id),
        pipeline_id=uuid.UUID(pipeline_id),
        connector_instance_id=uuid.UUID(connector_instance_id),
        poll_query=poll_query,
        condition_expression=condition_expression,
    )
    return await _dispatch_created_run(result, org_id=org_id, log_context="fire_polling_trigger")


async def fire_report_trigger(_ctx: dict[str, Any], *, report_id: str, org_id: str) -> dict[str, Any]:
    """Per-item report fire job — generate + deliver (SAQ bounded job)."""
    from modulo.core import cron_helpers as _ch

    return await _ch.fire_report_trigger(report_id=uuid.UUID(report_id), org_id=uuid.UUID(org_id))


async def fire_ongoing_trigger(
    _ctx: dict[str, Any],
    *,
    trigger_id: str,
    org_id: str,
    pipeline_id: str,
    latest_snapshot_id: str = "",
) -> dict[str, Any]:
    """Per-item ongoing fire job — top up + dispatch the created runs (SAQ).

    Thin wrapper mirroring ``fire_polling_trigger``: delegates to
    ``cron_helpers.fire_ongoing_trigger``, which performs the DB top-up in one
    transaction and the queue dispatch post-commit inside its own body (the seam
    lives in cron_helpers so there is one place to test). The dispatch outcomes
    are already attached to the returned summary; failures are logged there too.
    """
    from modulo.core import cron_helpers as _ch

    return await _ch.fire_ongoing_trigger(
        trigger_id=uuid.UUID(trigger_id),
        org_id=uuid.UUID(org_id),
        pipeline_id=uuid.UUID(pipeline_id),
        latest_snapshot_id=latest_snapshot_id or "",
    )


async def fire_suite_run_trigger(
    ctx: dict[str, Any],
    *,
    trigger_id: str,
    org_id: str,
    pipeline_id: str,
    cron_expression: str = "",
    snapshot_id: str = "",
) -> dict[str, Any]:
    """Per-item fire job for a ``run_kind='suite_run'`` trigger (SAQ, FAR-377).

    Builds a ``pending`` SuiteRun via ``cron_helpers.fire_suite_run_trigger``
    (which enforces the suite-run concurrency + spend pools and writes NO
    trigger-watch event), then — post-commit — enqueues the
    ``execute_suite_run`` job. A fired run is dispatched exactly like a fired
    pipeline run, but runs through the ``execute_suite_run`` SAQ job instead of
    ``execute_run``.
    """
    from modulo.core import cron_helpers as _ch

    result = await _ch.fire_suite_run_trigger(
        trigger_id=uuid.UUID(trigger_id),
        org_id=uuid.UUID(org_id),
        pipeline_id=uuid.UUID(pipeline_id),
    )
    if result.get("status") == "fired" and result.get("suite_run_id"):
        try:
            await _enqueue_suite_run_execution(result["suite_run_id"], org_id)
            result["dispatched"] = "enqueued"
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("fire_suite_run_trigger: enqueue failed for suite_run %s", result.get("suite_run_id"))
            # Terminalise the freshly-committed (``pending``) SuiteRun in its OWN
            # transaction so it is never stranded ``pending`` forever. Re-raising
            # would only retry the fire job and create a *duplicate* pending run
            # (the fire is not idempotent in run creation), re-introducing the
            # exact stranded-``pending`` state the transaction-boundary fix
            # eliminated. Mirrors the production path: a run that cannot be
            # dispatched must land ``failed`` with an error_detail + Error
            # Dashboard ingest (FAR-377 reviewer finding).
            await _fail_suite_run_on_enqueue_error(result["suite_run_id"], org_id)
            result["dispatched"] = "enqueue_failed"
    return result


async def _fail_suite_run_on_enqueue_error(suite_run_id: str, org_id: str) -> None:
    """Terminalise a ``pending`` SuiteRun whose ``execute_suite_run`` job could not be enqueued.

    Called from ``fire_suite_run_trigger`` when ``_enqueue_suite_run_execution``
    raises (Redis down / SAQ unavailable). The SuiteRun was already committed as
    ``pending`` by ``cron_helpers.fire_suite_run_trigger``; without this it would
    sit ``pending`` forever (nothing reconciles stuck ``pending`` suite_runs).
    Promotes ``pending -> running -> failed`` (the legal edge) via ``_fail_run``
    and ingests the failure to the Error Dashboard, isolated in its own
    committed transaction so the fire job itself can still return cleanly.
    """
    from modulo.core.eval_engine.execute_suite_run import _fail_run
    from modulo.db.models.eval_suite_run import SuiteRun
    from modulo.db.rls import set_rls_org as _set_rls

    rid = uuid.UUID(suite_run_id)
    oid = uuid.UUID(org_id)
    factory = _make_session_factory()
    try:
        async with factory() as session, session.begin():
            await _set_rls(session, oid)
            run = await session.get(SuiteRun, rid)
            if run is None or run.organisation_id != oid:
                _log.warning("fire_suite_run_trigger: cannot terminalise missing/cross-org suite_run %s", rid)
                return
            await _fail_run(
                session,
                run,
                "SuiteRun could not be dispatched (SAQ/Redis enqueue failed); the run was never executed.",
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("fire_suite_run_trigger: failed to terminalise suite_run %s after enqueue error", rid)


async def _enqueue_suite_run_execution(suite_run_id: str, org_id: str) -> str | None:
    """Enqueue the ``execute_suite_run`` job on the runs queue (no Run capacity gate).

    A SuiteRun is not a pipeline ``Run``, so ``dispatch_run`` (which loads a
    ``Run`` and checks pipeline/org capacity) does not apply. The job key is
    deterministic so SAQ dedupe never double-runs a suite, and a re-fire uses a
    fresh suite_run anyway (each fire is a distinct SuiteRun row).
    """
    from saq.queue.redis import RedisQueue

    settings = get_settings()
    redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
        max_connections=settings.saq_redis_pool_size,
    )
    queue = RedisQueue(redis_client, name=settings.saq_runs_queue)
    try:
        job = await queue.enqueue(
            "modulo.core.saq_worker.execute_suite_run",
            key=f"suite_run:{suite_run_id}",
            timeout=300,
            heartbeat=30,
            retries=2,
            ttl=300,
            suite_run_id=suite_run_id,
            org_id=org_id,
        )
        return job.id if job is not None else None
    finally:
        with contextlib.suppress(Exception):
            await redis_client.aclose()


async def execute_suite_run(ctx: dict[str, Any], *, suite_run_id: str, org_id: str) -> dict[str, Any]:
    """SAQ ``execute_suite_run`` job — run a scheduled SuiteRun to terminal (FAR-377).

    Called for a ``run_kind='suite_run'`` trigger that a fire job (or the
    ``fire_suite_run`` dispatch path) enqueued. Loads the persisted SuiteRun
    (carrying the immutable baseline tuple + any config-derived ceiling from
    ``extra``), executes it end-to-end (pending -> terminal) via the
    ``execute_suite_run`` runner, and returns the terminal stats. On an
    orchestration failure (typed OR raw DB error) the runner transitions the run
    to ``failed``, but that write happens INSIDE ``session.begin()`` and is
    ROLLED BACK when the runner re-raises — so the failure is re-persisted in its
    OWN committed transaction (``_persist_suite_run_execution_failure``) before the
    job re-raises for the SAQ ``after_process`` hook (the monitored failure sink).
    A persistent failure therefore leaves the run ``failed`` with
    ``error_detail`` populated, never stranded ``pending``.
    """
    from modulo.core.eval_engine.execute_suite_run import execute_suite_run as _run_exec
    from modulo.db.models.eval_suite_run import SuiteRun, is_terminal
    from modulo.db.rls import set_rls_org

    rid = uuid.UUID(suite_run_id)
    oid = uuid.UUID(org_id)
    factory = _make_session_factory()
    try:
        async with factory() as session, session.begin():
            await set_rls_org(session, oid)
            run = await session.get(SuiteRun, rid)
            if run is None:
                _log.warning("SAQ execute_suite_run: suite_run %s not found", rid)
                return {"status": "missing"}
            # modulo_app is BYPASSRLS, so ``session.get`` is NOT org-scoped — the
            # explicit predicate is the isolation control. Verify the loaded run
            # belongs to the job's org before executing it (defense-in-depth):
            # a cross-org suite_run_id must never be executed.
            if run.organisation_id != oid:
                _log.warning("SAQ execute_suite_run: suite_run %s belongs to a different org", rid)
                return {"status": "missing"}
            # Terminal short-circuit (FAR-377 reviewer minor): the enqueued job uses
            # ``retries=2``, so a run that already reached a terminal state can be
            # re-invoked on retry. Re-evaluating the whole dataset only to fail the
            # illegal ``failed -> completed`` transition is wasted work + a noisy
            # error path — close it cheaply instead.
            if is_terminal(run.state):
                _log.info(
                    "SAQ execute_suite_run: suite_run %s already terminal (%s) - skipping re-execution",
                    rid,
                    run.state,
                )
                return {"status": "already_terminal", "state": run.state}
            extra = run.extra or {}
            suite_ceiling_raw = extra.get("suite_ceiling")
            run_kwargs: dict[str, Any] = {
                "entity_thresholds": extra.get("entity_thresholds"),
                "scenario_inputs": extra.get("scenario_inputs"),
                "eval_definition_version": int(extra.get("eval_definition_version", 1)),
            }
            # ``cost_per_llm_case`` is JSON-decoded from ``extra`` (may be a
            # str/int/float). Coerce to ``Decimal`` (the ledger arithmetic needs
            # it); when absent, leave it to the runner's default rather than
            # passing ``None`` (which would override the default AND feed ``None``
            # into the ledger).
            cost_per = extra.get("cost_per_llm_case")
            if cost_per is not None:
                run_kwargs["cost_per_llm_case"] = Decimal(str(cost_per))
            # ``suite_ceiling`` is ALSO JSON-decoded (a str/int/float). The
            # runner only coerces it when the kwarg is ``None`` (it falls back to
            # ``run.extra``), so a raw str passed here would bypass that coercion
            # and raise ``Decimal >= str`` mid-evaluation. Coerce it the same way
            # as ``cost_per_llm_case``; when absent, omit it so the runner reads
            # the (already-``None``) value from ``run.extra``.
            if suite_ceiling_raw is not None:
                run_kwargs["suite_ceiling"] = Decimal(str(suite_ceiling_raw))
            stats = await _run_exec(session, run, **run_kwargs)
            run.extra = {**extra, "execution": stats}
            await session.flush()
        _log.info(
            "saq.execute_suite_run.done",
            extra={"suite_run_id": str(rid), "state": stats.get("state"), "org": str(oid)},
        )
        return stats
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("SAQ execute_suite_run failed for suite_run %s", rid)
        # Transaction-boundary fix (FAR-377): ``execute_suite_run`` (the runner)
        # transitions the run to ``failed`` + populates ``error_detail`` + ingests
        # the Error-Ingestion event, but it runs INSIDE ``session.begin()`` above.
        # When the runner re-raises (an orchestration failure — typed OR a raw DB
        # error), the async context manager ROLLS BACK that transaction, discarding
        # the ``failed`` transition + ``error_detail`` + event. The run would be
        # stranded ``pending`` forever — never terminal, never surfaced. Persist
        # the failure in a FRESH session/transaction here so it survives the
        # rollback, then re-raise for the after_process sink.
        await _persist_suite_run_execution_failure(factory, rid, oid, exc)
        raise


async def _persist_suite_run_execution_failure(
    factory: Any, suite_run_id: uuid.UUID, org_id: uuid.UUID, exc: Exception
) -> None:
    """Persist a SuiteRun orchestration failure in its OWN committed transaction.

    Called from ``execute_suite_run``'s outer handler AFTER the runner's execution
    transaction rolled back. Re-loads the freshly-persisted run (now ``pending``
    again — the rolled-back transaction also discarded the ``pending -> running``
    transition) and terminalises it to ``failed`` with ``error_detail`` + the
    Error-Ingestion event, then commits. ``_fail_run`` promotes a ``pending`` run
    to ``running`` first so the ``failed`` edge is legal, and isolates the ingest
    sink in a savepoint so it can never roll back the terminal transition.

    Best-effort: if this itself fails (e.g. the DB is down), it is logged and the
    caller still re-raises so SAQ's after_process sink sees the original error.
    """
    from modulo.core.eval_engine.execute_suite_run import _fail_run, _failure_detail
    from modulo.db.models.eval_suite_run import SuiteRun
    from modulo.db.rls import set_rls_org as _set_rls

    detail = _failure_detail(exc)
    try:
        async with factory() as session, session.begin():
            await _set_rls(session, org_id)
            run = await session.get(SuiteRun, suite_run_id)
            if run is None or run.organisation_id != org_id:
                _log.warning(
                    "SAQ execute_suite_run: cannot persist failure, suite_run %s missing or cross-org",
                    suite_run_id,
                )
                return
            await _fail_run(session, run, detail)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("SAQ execute_suite_run: failed to persist suite_run failure %s", suite_run_id)


# ---------------------------------------------------------------------------
# Job functions — system worker (plan F1 / F3c / PR B step 6)
# ---------------------------------------------------------------------------


async def fire_due_triggers(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — read due rows, atomic next_fire_at advance, enqueue fire jobs."""
    from modulo.core import cron_helpers as _ch

    return await _ch.fire_due_triggers()


async def dispatcher_reconcile(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — re-dispatch runs whose SAQ job is missing (every 60s)."""
    from modulo.core import cron_helpers as _ch

    return await _ch.dispatcher_reconcile()


async def claim_expiry(_ctx: dict[str, Any]) -> dict[str, Any]:
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


async def hitl_overdue(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — dispatch ``hitl_overdue`` notifications for HITL gates that
    have been waiting past the overdue threshold (idempotent per claim).
    """
    from modulo.core.hitl_manager.overdue_warning import dispatch_overdue_notifications
    from modulo.core.notifier import Notifier

    settings = get_settings()
    factory = _make_session_factory()
    notifier: Notifier | None = None
    try:
        notifier = Notifier(_get_async_engine(), settings.fernet_key)
    except Exception:
        _log.exception("hitl_overdue: notifier init failed — overdue dispatch still runs")
    dispatched = await dispatch_overdue_notifications(factory, notifier=notifier)
    return {"dispatched": len(dispatched)}


async def retention_cleanup(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — batch-delete terminal runs and old LangGraph checkpoint rows.

    Deletes terminal ``runs`` rows older than the retention window (via
    ``batch_delete_old_terminal_runs``) AND purges LangGraph checkpoint rows
    (``checkpoints``, ``checkpoint_blobs``, ``checkpoint_writes``) older than
    the retention window (via ``batch_delete_langgraph_checkpoints``). The two
    purges run in SEPARATE transactions so a failure in the checkpoint purge
    can never roll back the already-executed runs purge.

    The checkpoint purge is tolerant of a missing saver schema: the system
    worker's cron can fire before the app boot creates the checkpoint tables /
    ``created_at`` columns, in which case ``ProgrammingError`` (the SQLAlchemy
    wrapper for the DBAPI's missing-table/column errors, e.g. psycopg's
    ``UndefinedTable``/``UndefinedColumn``) is caught, logged as a warning, and
    the job still reports the runs purge count (``checkpoints_deleted=0``)
    without failing — the app boot creates the schema later. The retention
    session is system-scoped (modulo_system role, LOGIN, BYPASSRLS) — checkpoint
    retention is cross-org by design and operates on the saver's unqualified
    tables.
    """
    from sqlalchemy.exc import ProgrammingError

    from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints
    from modulo.db.crud.run import batch_delete_old_terminal_runs

    factory = _make_system_session_factory()
    async with factory() as session, session.begin():
        deleted = await batch_delete_old_terminal_runs(session)

    checkpoints_deleted = 0
    try:
        async with _make_system_session_factory()() as session, session.begin():
            checkpoints_deleted = await batch_delete_langgraph_checkpoints(session)
    except ProgrammingError:
        _log.warning(
            "saq.retention_cleanup.checkpoint_schema_missing",
            exc_info=True,
        )

    if deleted or checkpoints_deleted:
        _log.info(
            "saq.retention_cleanup.deleted_old_runs",
            extra={"count": deleted, "checkpoints_deleted": checkpoints_deleted},
        )
    return {"deleted": deleted, "checkpoints_deleted": checkpoints_deleted}


async def webhook_dedup_cleanup(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — purge old webhook trigger events (30-day retention).

    The system session factory is ``autobegin=False`` (the codebase DI
    convention), so every batch needs an explicit transaction: the first
    ``session.execute`` would otherwise raise ``InvalidRequestError: Autobegin
    is disabled on this Session`` and the hourly cron would fail on every tick.
    The transaction must begin PER BATCH (not once around the loop) because
    ``cleanup_old_webhook_events`` commits at the end of each pass.
    """
    from modulo.core.cleanup_jobs.webhook_dedup_cleanup import BATCH_SIZE, cleanup_old_webhook_events

    total = 0
    async with _make_session_factory()() as session:
        while True:
            async with session.begin():
                deleted = await cleanup_old_webhook_events(session)
            total += deleted
            if deleted < BATCH_SIZE:
                break
    return {"deleted": total}


async def trigger_events_cleanup(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — age-based retention for trigger_events (90-day default).

    Deletes ``trigger_events`` rows whose ``received_at`` is older than the
    retention window (default 90 days, aligned with the run-retention policy)
    in bounded batches. The retention comfortably exceeds the webhook replay
    window, so replayable events are never purged.

    Mirrors ``webhook_dedup_cleanup``: the system session factory is
    ``autobegin=False`` (the codebase DI convention), so every batch needs an
    explicit transaction — the first ``session.execute`` would otherwise raise
    ``InvalidRequestError: Autobegin is disabled on this Session``. The
    transaction must begin PER BATCH (not once around the loop) because
    ``cleanup_old_trigger_events`` commits at the end of each pass.
    """
    from modulo.core.cleanup_jobs.trigger_events_cleanup import BATCH_SIZE, cleanup_old_trigger_events

    total = 0
    async with _make_session_factory()() as session:
        while True:
            async with session.begin():
                deleted = await cleanup_old_trigger_events(session)
            total += deleted
            if deleted < BATCH_SIZE:
                break
    return {"deleted": total}


# Cross-process stats key for the stale-run recovery sweep (D1). The sweep runs
# in the SYSTEM WORKER process; /healthz/ready runs in the WEB process. Mirroring
# the dispatcher_reconcile stats pattern, the cron job persists its outcome here
# every tick and the health check reads this key to detect a silently dead sweep
# (stale > 15 min) or a never-run sweep.
STALE_RUN_RECOVERY_STATS_KEY = "saq:cron:stats:stale_run_recovery"
# The sweep runs every 5 min; a last_run_at older than 15 min means at least two
# ticks were missed -> report "stale" (advisory).
STALE_RUN_RECOVERY_STALE_SECONDS = 15 * 60


async def stale_run_recovery(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — legacy stale-run sweep, scoped to non-SAQ rows (F1).

    The sweep itself (``pipeline_execution.stale_run_recovery_sweep``) returns a
    plain integer count; this wrapper ALSO persists a stats dict to the shared
    Redis key so /healthz/ready can warn when the sweep is stale or missing.
    Best-effort: a persistence failure must never fail the sweep.
    """
    from modulo.core.pipeline_execution import stale_run_recovery_sweep

    recovered = await stale_run_recovery_sweep(_get_async_engine())
    stats: dict[str, Any] = {
        "last_run_at": datetime.now(UTC).isoformat(),
        "recovered": recovered,
    }
    try:
        from redis.asyncio import Redis as AsyncRedis

        redis_client = AsyncRedis.from_url(get_settings().redis_url, socket_connect_timeout=5)
        try:
            await redis_client.set(STALE_RUN_RECOVERY_STATS_KEY, json.dumps(stats))
        finally:
            with contextlib.suppress(Exception):
                await redis_client.aclose()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("saq_worker.stale_run_recovery_stats_persist_failed")
    return recovered


async def cost_probe(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — the cost-tracking probe (spec Â§4.7, every 5 min, retries=0).

    The verification canary for the ledger/report system: samples the N=50 most
    recent terminal runs per org, checks ``total == sum``, asserts the org-row
    WATCH, and evaluates the canonical rollback trigger (the probe rule + the
    duplicate-terminal flood with its cooldown). The heartbeat gauge turns a
    silently dead probe into a stale alert.
    """
    from modulo.core.cost_controller.probe import run_probe

    return await run_probe(_make_session_factory())


async def analytics_facts_maintenance(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — daily run-facts backfill + reconcile + retention (ADR 020).

    System cron: uses modulo_system role (LOGIN, BYPASSRLS) for cross-org access.
    modulo_app is NOBYPASSRLS. Non-Postgres backends no-op.
    """
    from modulo.core.analytics.maintenance import run_maintenance

    return await run_maintenance(_make_system_session_factory())


async def journey_reconcile(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — hourly bounded journey reconciliation sweep (FAR-143).

    Re-derives ``journeys`` evidence from terminal runs whose journey rows are
    MISSING or STALE (see ``modulo.core.lifecycle_map.reconcile``). System cron:
    uses modulo_system role (LOGIN, BYPASSRLS) for cross-org access. modulo_app
    is NOBYPASSRLS. The sweep is batch-bounded and idempotent, so an hourly
    tick simply drains whatever backlog remains.
    """
    from modulo.core.lifecycle_map.reconcile import reconcile_journeys

    async with _make_system_session_factory()() as session, session.begin():
        advanced = await reconcile_journeys(session)
    if advanced:
        _log.info("saq.journey_reconcile.advanced", extra={"advanced": advanced})
    return {"advanced": advanced}


async def check_missed_fire_alerts_cron(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — hourly missed-fire probe for silent low-cadence triggers.

    Delegates to :func:`modulo.core.error_tracking.check_missed_fire_alerts`,
    which alerts for active cron/polling triggers whose cadence is >= 1h and
    whose ``last_fired_at`` is stale (throttled by an in-memory cooldown).

    Uses the 5-field expression ``"0 * * * *"`` — NOT the 6-field form, which
    croniter parses with a leading seconds field and fires on the wrong cadence
    (the bug class documented on ``cost_probe`` / bug #680).
    """
    from modulo.core.error_tracking import check_missed_fire_alerts

    emitted = await check_missed_fire_alerts(_get_async_engine())
    if emitted:
        _log.info("saq.check_missed_fire_alerts.emitted", extra={"count": emitted})
    return {"emitted": emitted}


async def library_sync(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — periodic community-library sync (FAR-363).

    No-op when ``modulo_library_endpoint`` is empty (library not configured).
    Never raises (fail-open): ``sync_library`` already returns a result on every
    failure path and this wrapper adds a final guard so a session/DB failure
    cannot crash the worker. The last-good cached manifest survives a bad tick.
    """
    settings = get_settings()
    if not settings.modulo_library_endpoint:
        _log.info("saq.library_sync.disabled")
        return {"status": "disabled"}
    try:
        from modulo.core.library_sync import sync_library

        async with _make_session_factory()() as session:
            result = await sync_library(session)
        return {
            "status": "ok" if result.success else "failed",
            "entries_count": result.entries_count,
            "revoked_count": result.revoked_count,
            "error": result.error,
        }
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("saq.library_sync.failed")
        return {"status": "failed", "error": "unexpected cron failure"}


async def metrics_dump(ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — daily product analytics metrics dump (FAR-356).

    Builds an aggregate payload from all consenting orgs and POSTs it to
    the vendor endpoint.  Skips when: no consenting orgs or instance switch
    off.  Watermark advances only on full success.
    """
    from modulo.core.product_analytics.metrics_dump import metrics_dump as _run_dump

    return await _run_dump(ctx)


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
        "concurrency": get_settings().saq_worker_concurrency,
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


# ---------------------------------------------------------------------------
# System cron engine — dedicated modulo_system role (LOGIN, BYPASSRLS)
# ---------------------------------------------------------------------------

_SYSTEM_ASYNC_ENGINE: AsyncEngine | None = None


def _get_system_async_engine() -> AsyncEngine:
    """Engine for cross-org system crons using the modulo_system role.

    FAIL CLOSED when MODULO_SYSTEM_DATABASE_URL is not set: cross-org system
    crons require the modulo_system role (LOGIN, BYPASSRLS). Falling back to the
    regular engine would run them as modulo_app, which is NOBYPASSRLS (see
    bootstrap_role.py: the app role asserts ``rolbypassrls = false``), so any
    RLS-scoped read silently returns zero rows — a silent no-op that masks a
    misconfigured deployment. Instead, a clear ``RuntimeError`` is raised. The
    engine is created LAZILY on first use inside a cron invocation (never at
    worker startup), so this raise fails only the specific system cron — which
    SAQ logs — while the worker stays up to serve every other job.
    """
    global _SYSTEM_ASYNC_ENGINE
    if _SYSTEM_ASYNC_ENGINE is None:
        settings = get_settings()
        if settings.modulo_system_database_url:
            from sqlalchemy.ext.asyncio import create_async_engine

            effective_pool = _effective_db_pool_size(settings.saq_worker_db_pool_size, settings.saq_worker_concurrency)
            if effective_pool != settings.saq_worker_db_pool_size:
                _log.warning(
                    "saq_worker.db_pool_raised",
                    extra={
                        "configured_pool": settings.saq_worker_db_pool_size,
                        "effective_pool": effective_pool,
                        "concurrency": settings.saq_worker_concurrency,
                    },
                )
            _SYSTEM_ASYNC_ENGINE = create_async_engine(
                settings.modulo_system_database_url,
                pool_pre_ping=True,
                pool_size=effective_pool,
                max_overflow=0,
                connect_args={"ssl": False, "statement_cache_size": 0},
            )
        else:
            _log.error(
                "saq_worker.system_engine_misconfigured",
                extra={
                    "reason": (
                        "MODULO_SYSTEM_DATABASE_URL not set — refusing to run cross-org "
                        "system crons as modulo_app (NOBYPASSRLS); RLS-scoped reads would "
                        "silently return zero rows. Set the modulo_system role URL."
                    )
                },
            )
            raise RuntimeError(
                "MODULO_SYSTEM_DATABASE_URL is not set: cross-org system crons require "
                "the modulo_system role (LOGIN, BYPASSRLS). Refusing to fail open to "
                "modulo_app (NOBYPASSRLS), which would silently return zero rows."
            )
    return _SYSTEM_ASYNC_ENGINE


def _make_system_session_factory() -> Any:
    """Session factory for cross-org system crons (modulo_system role).

    Does NOT include the RLS reset hook — system crons operate cross-org
    intentionally and never call set_rls_org.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(_get_system_async_engine(), expire_on_commit=False, autobegin=False)


def _runs_functions() -> list[tuple[str, Any]]:
    """Functions registered on the ``runs`` worker.

    Names match the strings enqueued by dispatch_run and fire_due_triggers.
    """
    return [
        ("modulo.core.saq_worker.execute_run", execute_run),
        ("modulo.core.saq_worker.resume_run", resume_run),
        ("modulo.core.saq_worker.execute_suite_run", execute_suite_run),
        ("modulo.core.saq_worker.fire_cron_trigger", fire_cron_trigger),
        ("modulo.core.saq_worker.fire_polling_trigger", fire_polling_trigger),
        ("modulo.core.saq_worker.fire_report_trigger", fire_report_trigger),
        ("modulo.core.saq_worker.fire_ongoing_trigger", fire_ongoing_trigger),
        ("modulo.core.saq_worker.fire_suite_run_trigger", fire_suite_run_trigger),
    ]


def _system_functions() -> list[Any]:
    """Functions registered on the ``system`` worker (under their ``__qualname__``,
    which is the name SAQ's cron scheduler uses when enqueueing).
    """
    return [
        fire_due_triggers,
        dispatcher_reconcile,
        claim_expiry,
        hitl_overdue,
        retention_cleanup,
        webhook_dedup_cleanup,
        trigger_events_cleanup,
        stale_run_recovery,
        cost_probe,
        analytics_facts_maintenance,
        journey_reconcile,
        check_missed_fire_alerts_cron,
        library_sync,
        metrics_dump,
    ]


def _system_cron_jobs() -> list[CronJob[Any]]:
    """System cron jobs (plan F1) — all knobs explicit."""
    return [
        # fire_due_triggers: every 60s (croniter parses 5-field cron, so the
        # 30s intent is not achievable — every minute); the atomic next_fire_at
        # advance makes multi-machine ticks safe (unique=True only prevents overlap).
        CronJob(
            fire_due_triggers,
            cron=_CRON_EVERY_MINUTE,
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=3,
            ttl=300,
        ),
        # dispatcher_reconcile: every 60s (timeout=120 per plan F1).
        CronJob(
            dispatcher_reconcile,
            cron=_CRON_EVERY_MINUTE,
            unique=True,
            timeout=120,
            heartbeat=30,
            retries=3,
            ttl=300,
        ),
        # claim-expiry: every 60s — SAQ cron is the SOLE writer/notifier (F1).
        CronJob(
            claim_expiry,
            cron=_CRON_EVERY_MINUTE,
            unique=True,
            timeout=120,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # hitl-overdue: every 5 minutes — overdue thresholds are hour-scale, so
        # a 5-min tick keeps latency low while avoiding contention with the
        # claim-expiry sweep; unique so overlapping ticks cannot double-dispatch.
        CronJob(
            hitl_overdue,
            cron=_CRON_EVERY_5_MINUTES,
            unique=True,
            timeout=120,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # retention: hourly (matches the in-process _run_retention_loop cadence).
        CronJob(
            retention_cleanup,
            cron=_CRON_HOURLY,
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # webhook-dedup cleanup: hourly (matches _CLEANUP_INTERVAL_SECONDS).
        CronJob(
            webhook_dedup_cleanup,
            cron=_CRON_HOURLY,
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # trigger_events retention: hourly, unique=True so overlapping ticks
        # cannot interleave (bounded + idempotent — a second instance can only
        # find nothing left to delete).
        CronJob(
            trigger_events_cleanup,
            cron=_CRON_HOURLY,
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
            cron=_CRON_EVERY_5_MINUTES,
            unique=True,
            timeout=120,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # cost_probe: every 5 min, retries=0 (pinned — a dead probe is caught
        # separately by the heartbeat/staleness alert), unique=True so a second
        # overlapping instance cannot double-advance probe_state (Â§4.7).
        # NOTE: must be the 5-field form "*/5 * * * *" — croniter parses a
        # 6-field expression ("0 */5 * * * *") differently and the probe fires
        # per-5-hours instead of per-5-minutes (bug class #680).
        CronJob(
            cost_probe,
            cron=_CRON_EVERY_5_MINUTES,
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=0,
            ttl=300,
        ),
        # analytics_facts_maintenance: daily (idempotent — the anti-join +
        # ON CONFLICT DO NOTHING make overlap harmless), unique=True so a
        # second instance cannot double-run a maintenance day-slice.
        CronJob(
            analytics_facts_maintenance,
            cron="0 1 * * *",
            unique=True,
            timeout=600,
            heartbeat=60,
            retries=1,
            ttl=900,
        ),
        # journey_reconcile: hourly (bounded + idempotent — a second instance
        # cannot double-advance because a reconciled ref is no longer drift),
        # unique=True so overlapping ticks cannot interleave. The sweep drains
        # oldest-first across ticks.
        CronJob(
            journey_reconcile,
            cron=_CRON_HOURLY,
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # check_missed_fire_alerts: hourly (the probe only targets triggers
        # with a >= 1h cadence, so an hourly tick with its own cooldown is
        # ample). NOTE: must be the 5-field form "0 * * * *" — croniter parses
        # a 6-field expression with a leading seconds field differently (bug
        # class #680).
        CronJob(
            check_missed_fire_alerts_cron,
            cron=_CRON_HOURLY,
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=2,
            ttl=300,
        ),
        # library_sync: cadence derives from modulo_library_sync_interval_seconds
        # (default 300s -> every 5 min). Fail-open: the job never raises (a bad
        # tick is logged and the last-good cached manifest survives). unique=True
        # so overlapping ticks cannot double-write the singleton state row.
        CronJob(
            library_sync,
            cron=_sync_interval_to_cron(get_settings().modulo_library_sync_interval_seconds),
            unique=True,
            timeout=300,
            heartbeat=30,
            retries=1,
            ttl=300,
        ),
        # metrics_dump: ticks every 10 minutes (*/10 * * * *).  The per-instance
        # jitter gate (metrics_dump._should_dump_now) opens a 10-minute execution
        # window aligned to this grid, so each instance performs its daily dump on
        # exactly one tick, spread across a 6-hour window (36 grid slots).  The
        # cron must run on the SAME grid the gate is aligned to, or the dump may
        # never fire (FAR-356 review).  unique=True, long timeout (full org scan),
        # single retry, generous ttl.  Watermark advances only on full success.
        CronJob(
            metrics_dump,
            cron="*/10 * * * *",
            unique=True,
            timeout=600,
            heartbeat=60,
            retries=1,
            ttl=900,
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

    _probe_database()

    worker = Worker(**system_settings())
    loop = asyncio.new_event_loop()

    async def _set_duplicate_terminal_cooldown() -> None:
        """Set the flood cooldown on worker start so the probe (same process)
        does not auto-trigger a rollback on PR A's OWN rollout restart burst.
        """
        from modulo.core.cost_controller.probe import set_duplicate_terminal_cooldown

        try:
            await set_duplicate_terminal_cooldown(_make_session_factory())
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("saq_worker.cooldown_set_failed")

    _cooldown_task = loop.create_task(_set_duplicate_terminal_cooldown())
    _cooldown_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _worker_start() -> None:
        try:
            await worker.queue.connect()
            await reconcile_cron_registrations(cast(RedisQueue, worker.queue), worker.cron_jobs)
            await worker.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("SAQ system worker failed to start — exiting gracefully")
            sys.exit(1)
        finally:
            await worker.queue.disconnect()

    async def _shutdown(_app: Any) -> None:
        await worker.stop()

    queue = worker.queue
    app = create_app([queue])
    app.on_shutdown.append(_shutdown)

    loop.create_task(_worker_start()).add_done_callback(lambda fut: None if fut.cancelled() else fut.exception())
    web.run_app(app, host=_SYSTEM_WEB_HOST, port=_SYSTEM_WEB_PORT, loop=loop)


def main() -> None:
    """Entry point for ``python -m modulo.core.saq_worker`` (system worker)."""
    run_system_web()


if __name__ == "__main__":
    main()
