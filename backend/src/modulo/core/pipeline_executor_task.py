"""Celery task for consolidated pipeline execution.

All pipeline execution flows through a single Celery task registered
as ``modulo.pipeline.execute_run``.  Uses a persistent asyncio event
loop per prefork child and lazy, thread-safe engine singletons.

The stale-run recovery sweep shares the async engine -- it runs every
5 minutes as a beat task and does not compete with execution for pool slots.

Connection budget (per prefork child):
  Sync pool: pool_size + max_overflow (claims + heartbeats)
  Async pool: pool_size + max_overflow (pipeline execution)
  Total per child: (sync_N+sync_O) + (async_N+async_O)
  Enforced by Settings._check_connection_budget (per-child max = 16)
  Total per cluster: per_child_value x worker_count
    automated workers (4): default 10/child x 4 = 40 connections
    manual workers (2):    default 10/child x 2 = 20 connections
    beat:                  1 connection
    web app:               ~10-20 connections (separate pool)
  Postgres max_connections default: 100
  Budget at defaults: 40+20+1 = ~61 out of 100 (96 max at cap)
"""

import asyncio
import contextlib
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

try:
    import kombu.exceptions
    import redis.exceptions
    import sqlalchemy.exc
    from celery import Task
    from celery.signals import worker_process_init, worker_process_shutdown

    _CELERY_SIGNALS_AVAILABLE = True
except ImportError:
    import typing

    if typing.TYPE_CHECKING:
        import kombu.exceptions
        import redis.exceptions
        import sqlalchemy.exc
        from celery import Task
        from celery.signals import worker_process_init, worker_process_shutdown
    Task = object
    _CELERY_SIGNALS_AVAILABLE = False

_log = logging.getLogger(__name__)

_TASK_NAME = "modulo.pipeline.execute_run"

_sync_lock = threading.Lock()
_SYNC_ENGINE: Any = None
_ASYNC_ENGINE: AsyncEngine | None = None

_worker_loop: asyncio.AbstractEventLoop | None = None


def _get_settings() -> Any:
    from modulo.settings import get_settings

    return get_settings(fresh=True)


if _CELERY_SIGNALS_AVAILABLE:

    @worker_process_shutdown.connect
    def _dispose_worker_engines(**kwargs):
        """Dispose engine singletons on worker shutdown.

        Assumes Celery's graceful shutdown (default: wait for running tasks to
        finish). If a task holds a connection when dispose() fires, the
        connection becomes invalid mid-operation -- graceful shutdown avoids
        this race.
        """
        global _SYNC_ENGINE, _ASYNC_ENGINE
        if _SYNC_ENGINE is not None:
            try:
                _SYNC_ENGINE.dispose()
            except Exception:
                _log.exception("pipeline_executor._dispose_sync_engine")
            _SYNC_ENGINE = None
        if _ASYNC_ENGINE is not None:
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                loop.run_until_complete(_ASYNC_ENGINE.dispose())
            except Exception:
                _log.exception("pipeline_executor._dispose_async_engine")
            _ASYNC_ENGINE = None


def _get_engines():
    global _SYNC_ENGINE, _ASYNC_ENGINE
    if _ASYNC_ENGINE is not None:
        return _SYNC_ENGINE, _ASYNC_ENGINE
    s = _get_settings()
    sync_url = (
        str(s.database_url)
        .replace("+asyncpg", "+psycopg")
        .replace("+aiomysql", "+mysqldb")
        .replace("+aiosqlite", "+pysqlite")
    )
    _SYNC_ENGINE = create_engine(
        sync_url,
        pool_size=s.modulo_celery_db_pool_sync_size,
        max_overflow=s.modulo_celery_db_pool_sync_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    _ASYNC_ENGINE = create_async_engine(
        s.database_url,
        pool_size=s.modulo_celery_db_pool_async_size,
        max_overflow=s.modulo_celery_db_pool_async_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    return _SYNC_ENGINE, _ASYNC_ENGINE


def _get_sync_engine():
    return _get_engines()[0]


def _get_async_engine():
    return _get_engines()[1]


def reset_engines():
    """Reset engine singletons. Call in test setup to isolate test cases."""
    global _SYNC_ENGINE, _ASYNC_ENGINE
    for e in (_SYNC_ENGINE, _ASYNC_ENGINE):
        if e is not None:
            with contextlib.suppress(Exception):
                e.dispose()
    _SYNC_ENGINE = None
    _ASYNC_ENGINE = None


if _CELERY_SIGNALS_AVAILABLE:

    @worker_process_init.connect
    def _init_worker(**kw: Any) -> None:
        global _worker_loop
        reset_engines()
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
        _log.info("pipeline_executor_task: worker process initialised")

    @worker_process_shutdown.connect
    def _shutdown_worker(**kw: Any) -> None:
        global _SYNC_ENGINE, _ASYNC_ENGINE, _worker_loop
        if _SYNC_ENGINE is not None:
            try:
                _SYNC_ENGINE.dispose()
            except Exception:
                _log.exception("pipeline_executor._shutdown_sync_engine")
        if _ASYNC_ENGINE is not None and _worker_loop is not None:
            try:
                _worker_loop.run_until_complete(_ASYNC_ENGINE.dispose())
            except Exception:
                _log.exception("pipeline_executor._shutdown_async_engine")
        if _worker_loop is not None:
            try:
                _worker_loop.close()
            except Exception:
                _log.exception("pipeline_executor._close_worker_loop")
        _log.info("pipeline_executor_task: worker process shut down")


def dispatch(run_id: str, org_id: str, queue: str) -> None:
    """Dispatch a run to Celery and record dispatched_at (best-effort)."""
    from modulo.celery_app import app as celery_app

    celery_app.send_task(
        _TASK_NAME,
        args=[str(run_id), str(org_id)],
        queue=queue,
        retry=True,
        retry_policy={"max_retries": 3, "interval_start": 1, "interval_step": 2, "interval_max": 10},
    )
    try:
        with _get_sync_engine().connect() as c, c.begin():
            c.execute(
                text("UPDATE runs SET dispatched_at=now() WHERE id=:rid"),
                {"rid": str(run_id)},
            )
    except Exception:
        _log.warning("Failed to record dispatched_at for %s", run_id)


class ExecuteRunTask(Task):
    """Celery task that executes a single pipeline run end-to-end.

    Claim semantics (via SQL) ensure at-most-once execution; heartbeat
    updates keep the run alive.  A stale-running recovery sweep in the
    beat scheduler catches workers that crash without rejection.
    """

    name = _TASK_NAME
    autoretry_for = (
        kombu.exceptions.OperationalError,
        redis.exceptions.ConnectionError,
        sqlalchemy.exc.OperationalError,
    )
    max_retries = 3
    default_retry_delay = 60
    retry_backoff = True
    soft_time_limit = 870
    hard_time_limit = 900
    track_started = True
    acks_late = True
    reject_on_worker_lost = True

    def run(self, run_id: str, org_id: str):
        global _worker_loop
        if _worker_loop is None:
            _worker_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_worker_loop)
        _worker_loop.run_until_complete(_do_execute(run_id, org_id, self))

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        run_id = args[0] if args else None
        org_id = args[1] if len(args) > 1 else None
        if not run_id or not org_id:
            return
        _log.error("ExecuteRunTask failed for run %s: %s", run_id, exc)
        try:
            settings = _get_settings()
            url = str(settings.database_url).replace("+asyncpg", "+psycopg")
            engine = create_engine(url, pool_size=1, pool_pre_ping=True)
            with engine.connect() as c:
                c.execute(
                    text(
                        "UPDATE runs SET status='failed', error_code=:code, completed_at=now() "
                        "WHERE id=:rid AND organisation_id=:oid "
                        "AND status NOT IN ('completed', 'cancelled')"
                    ),
                    {"code": "task_failure", "rid": run_id, "oid": org_id},
                )
                c.commit()
            engine.dispose()
        except Exception:
            _log.exception("on_failure handler failed for run %s", run_id)


async def _do_execute(run_id: str, org_id: str, task_instance: Task):
    """Execute a single pipeline run from claim through completion."""
    aeng = _get_async_engine()
    rid = uuid.UUID(run_id)
    oid = uuid.UUID(org_id)

    claimed = _claim_run(str(rid), str(oid))
    if not claimed:
        _log.warning("Run %s not claimed — already handled or in wrong state", run_id)
        return

    cur, executor = await _load_and_setup(aeng, rid, oid)
    if cur is None:
        return

    heartbeat_task: asyncio.Task | None = None
    try:
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(str(rid), str(oid)),
            name=f"heartbeat-{run_id}",
        )
        await executor.execute(run_id=rid, org_id=oid, input_payload=cur.input_payload or {})
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Pipeline execution failed for run %s", run_id)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    await _mark_complete(aeng, str(rid), str(oid))


def _claim_run(run_id: str, org_id: str) -> bool:
    """Claim a pending or stale-running run via atomic SQL update.

    Returns True if the row was claimed, False if already handled.
    """
    try:
        with _get_sync_engine().connect() as c, c.begin():
            result = c.execute(
                text(
                    "UPDATE runs SET status='running', heartbeat_at=now(), claim_count=claim_count+1 "
                    "WHERE id=:rid AND organisation_id=:oid "
                    "AND (status = 'pending' "
                    "     OR (status = 'running' AND heartbeat_at < now() - interval '3 minutes')) "
                    "AND claim_count < 5 "
                    "RETURNING id"
                ),
                {"rid": run_id, "oid": org_id},
            )
            return result.fetchone() is not None
    except Exception:
        _log.exception("Claim failed for run %s", run_id)
        return False


async def _load_and_setup(aeng: AsyncEngine, rid: uuid.UUID, oid: uuid.UUID):
    """Load the Run and create PipelineExecutor with checkpointer."""
    from modulo.core.pipeline_engine.executor import PipelineExecutor
    from modulo.db.crud.run import get_run

    factory = async_sessionmaker(aeng, expire_on_commit=False)
    async with factory() as session, session.begin():
        await _set_rls_org(session, oid)
        cur = await get_run(session, rid)
        if cur is None:
            _log.warning("Run %s not found during load", rid)
            return None, None

    settings = _get_settings()
    conn_string = str(settings.database_url).replace("+asyncpg", "+psycopg")
    executor = PipelineExecutor(aeng, checkpointer_conn_string=conn_string)
    return cur, executor


async def _heartbeat_loop(run_id: str, org_id: str):
    """Periodic heartbeat every 30s to keep the run alive."""
    aeng = _get_async_engine()
    while True:
        try:
            await asyncio.sleep(30)
            async with aeng.connect() as c:
                await c.execute(
                    text("SELECT set_config('app.organisation_id', :val, true)"),
                    {"val": org_id},
                )
                await c.execute(
                    text("UPDATE runs SET heartbeat_at=now() WHERE id=:rid"),
                    {"rid": run_id},
                )
                await c.commit()
        except asyncio.CancelledError:
            break
        except Exception:
            _log.warning("Heartbeat failed for run %s", run_id)


async def _mark_complete(aeng: AsyncEngine, run_id: str, org_id: str):
    """Mark the run as completed if it's still running (don't overwrite failure/cancellation)."""
    factory = async_sessionmaker(aeng, expire_on_commit=False)
    async with factory() as session, session.begin():
        await _set_rls_org(session, uuid.UUID(org_id))
        from modulo.db.crud.run import get_run

        cur = await get_run(session, uuid.UUID(run_id))
        if cur is not None and cur.status == "running":
            cur.status = "completed"
            cur.completed_at = datetime.now(UTC)


async def _set_rls_org(session, org_id: uuid.UUID):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        await session.execute(
            text("SELECT set_config('app.organisation_id', :val, true)"),
            {"val": str(org_id)},
        )
    else:
        session.info["organisation_id"] = org_id


class StaleRunRecoveryTask(Task):
    """Beat periodic task that recovers stale runs every 5 minutes.

    Handles two scenarios:
      1. Never-dispatched pending runs (created in error, no worker ever picked them up)
      2. Stale running runs (worker crashed without Celery detecting the loss)
    """

    name = "modulo.pipeline.stale_run_recovery"
    ignore_result = True

    def run(self) -> dict[str, Any]:
        return asyncio.run(_stale_run_recovery_sweep())


async def _stale_run_recovery_sweep() -> dict[str, Any]:
    """Sweep stale pending and running pipeline runs.

    Uses the shared async engine (not a dedicated engine) -- the sweep is a
    periodic beat task that runs every 5 minutes and does not compete with
    execution for pool slots.

    - Pending runs older than 5 minutes with no ``dispatched_at`` are marked
      ``failed`` with ``never_dispatched``.
    - Running runs with a heartbeat older than 10 minutes and 5+ claims are
      marked ``failed`` with ``worker_lost``.
    """
    _, async_engine = _get_engines()
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
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


try:
    from modulo.celery_app import get_celery_app as _get_celery_app

    _celery_app = _get_celery_app()
    _celery_app.register_task(ExecuteRunTask())
    _celery_app.register_task(StaleRunRecoveryTask())
except Exception:
    _log.warning("Could not register Celery tasks — Celery may not be configured")
