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
import logging
import threading
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Session

from modulo.core.pipeline_execution import execute_run, stale_run_recovery_sweep

try:
    import kombu
    import kombu.exceptions
    import redis.exceptions
    import sqlalchemy.exc
    from celery import Task
    from celery.signals import worker_process_init, worker_process_shutdown

    _CELERY_SIGNALS_AVAILABLE = True
except ImportError:
    import typing

    if typing.TYPE_CHECKING:
        import kombu.exceptions  # type: ignore[import-untyped]
        import redis.exceptions
        import sqlalchemy.exc
        from celery import Task
        from celery.signals import worker_process_init, worker_process_shutdown
    Task = object
    _CELERY_SIGNALS_AVAILABLE = False

_log = logging.getLogger(__name__)

_TASK_NAME = "modulo.pipeline.execute_run"

_engine_lock = threading.Lock()
_SYNC_ENGINE: Any = None
_ASYNC_ENGINE: AsyncEngine | None = None

_worker_loop: asyncio.AbstractEventLoop | None = None


def _get_settings() -> Any:
    from modulo.settings import get_settings

    return get_settings()


def _get_engines() -> tuple[Engine, AsyncEngine]:
    """Create sync + async engines for this prefork child.

    NOTE: Only safe with --pool=prefork (default). gevent/eventlet/solo pools
    may create race conditions on the module-level globals. The default Celery
    pool type is prefork -- no other pool type is supported for these engines.
    """
    global _SYNC_ENGINE, _ASYNC_ENGINE
    if _ASYNC_ENGINE is not None:
        return _SYNC_ENGINE, _ASYNC_ENGINE
    with _engine_lock:
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
            pool_timeout=5,
            connect_args={"connect_timeout": s.modulo_celery_db_pool_connect_timeout},
        )
        _ASYNC_ENGINE = create_async_engine(
            s.database_url,
            pool_size=s.modulo_celery_db_pool_async_size,
            max_overflow=s.modulo_celery_db_pool_async_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_timeout=5,
            connect_args={"timeout": s.modulo_celery_db_pool_connect_timeout, "ssl": False},
        )
        _log.info(
            "Engines created: sync(pool=%d, overflow=%d, timeout=%ds) async(pool=%d, overflow=%d, timeout=%ds)",
            s.modulo_celery_db_pool_sync_size,
            s.modulo_celery_db_pool_sync_overflow,
            s.modulo_celery_db_pool_connect_timeout,
            s.modulo_celery_db_pool_async_size,
            s.modulo_celery_db_pool_async_overflow,
            s.modulo_celery_db_pool_connect_timeout,
        )
    return _SYNC_ENGINE, _ASYNC_ENGINE


def _get_sync_engine() -> Engine:
    return _get_engines()[0]


def _get_async_engine() -> AsyncEngine:
    return _get_engines()[1]


def reset_engines() -> None:
    """Reset engine singletons. Call in test setup to isolate test cases."""
    global _SYNC_ENGINE, _ASYNC_ENGINE
    for e in (_SYNC_ENGINE, _ASYNC_ENGINE):
        if e is not None:
            try:
                e.dispose()
            except Exception:
                _log.exception("pipeline_executor.reset_engines")
    _SYNC_ENGINE = None
    _ASYNC_ENGINE = None


if _CELERY_SIGNALS_AVAILABLE:

    @worker_process_init.connect  # type: ignore[untyped-decorator]
    def _init_worker(**kw: Any) -> None:
        global _worker_loop
        reset_engines()
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
        _log.info("pipeline_executor_task: worker process initialised")

    @worker_process_shutdown.connect  # type: ignore[untyped-decorator]
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
    from modulo.celery_app import get_celery_app

    celery_app = get_celery_app()

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


class ExecuteRunTask(Task):  # type: ignore[misc]
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

    def run(self, run_id: str, org_id: str) -> None:
        global _worker_loop
        if _worker_loop is None:
            _worker_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_worker_loop)
        _worker_loop.run_until_complete(
            execute_run(
                sync_engine=_get_sync_engine(),
                async_engine=_get_async_engine(),
                run_id=run_id,
                org_id=org_id,
                legacy_claim=True,
            )
        )

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:  # type: ignore[no-untyped-def]
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


class StaleRunRecoveryTask(Task):  # type: ignore[misc]
    """Beat periodic task that recovers stale runs every 5 minutes.

    Handles two scenarios:
      1. Never-dispatched pending runs (created in error, no worker ever picked them up)
      2. Stale running runs (worker crashed without Celery detecting the loss)

    Delegates to the shared sweep in modulo.core.pipeline_execution.
    """

    name = "modulo.pipeline.stale_run_recovery"
    ignore_result = True

    def run(self) -> dict[str, Any]:
        return asyncio.run(stale_run_recovery_sweep(_get_async_engine()))


class SchedulerDBError(Exception):
    """Raised when a scheduler DB query fails transiently."""

    pass


def _make_sync_url(database_url: str) -> str:
    """Convert async DB URL to sync by replacing async driver with sync equivalent."""
    return (
        database_url.replace("+asyncpg", "+psycopg").replace("+aiomysql", "+mysqldb").replace("+aiosqlite", "+pysqlite")
    )


_sync_beat_engine = None
_sync_beat_lock = threading.Lock()


def get_beat_sync_session() -> Session:
    """Return a sync SQLAlchemy session for the Celery beat scheduler.

    Uses a dedicated engine separate from the worker's sync pool to avoid
    contention between beat polling and task execution.
    """
    global _sync_beat_engine
    if _sync_beat_engine is None:
        with _sync_beat_lock:
            if _sync_beat_engine is None:
                from modulo.settings import get_settings

                s = get_settings()
                sync_url = _make_sync_url(str(s.database_url))
                _sync_beat_engine = create_engine(
                    sync_url,
                    pool_size=s.modulo_celery_db_pool_sync_size,
                    max_overflow=s.modulo_celery_db_pool_sync_overflow,
                    pool_pre_ping=True,
                    pool_recycle=300,
                    pool_use_lifo=False,  # FIFO
                    pool_timeout=s.modulo_celery_db_pool_sync_timeout,
                )
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=_sync_beat_engine)()


def dispose_beat_sync_engine() -> None:
    global _sync_beat_engine
    if _sync_beat_engine is not None:
        _sync_beat_engine.dispose()
        _sync_beat_engine = None


try:
    from modulo.celery_app import get_celery_app as _get_celery_app

    _celery_app = _get_celery_app()
    _celery_app.register_task(ExecuteRunTask())
    _celery_app.register_task(StaleRunRecoveryTask())
except Exception:
    _log.warning("Could not register Celery tasks — Celery may not be configured")
