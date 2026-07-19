"""Application-level background worker for pipeline execution.

Started during the FastAPI lifespan and lives outside any request's ASGI
scope, so background pipeline execution is never cancelled by scope teardown.

Usage:
    worker = BackgroundPipelineWorker(database_url, checkpointer_conn_string)
    await worker.start()   # during lifespan startup
    worker.submit(...)     # from request handlers
    await worker.stop()    # during lifespan shutdown
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.run import update_run_status
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

_POOL_SIZE = 3
_SEMAPHORE_VALUE = _POOL_SIZE
_MAX_OVERFLOW = 6
_POOL_TIMEOUT = 10
_CONSUMER_STOP_TIMEOUT = 5.0
_RUNNING_JOBS_DRAIN_TIMEOUT = 30.0


@dataclass
class PipelineJob:
    run_id: uuid.UUID
    org_id: uuid.UUID
    input_payload: dict[str, Any]


class BackgroundPipelineWorker:
    """Manages background pipeline execution via a persistent consumer loop.

    Owns its own database engine to isolate background execution from
    request-scoped connections. Submissions go to an ``asyncio.Queue``
    and are consumed by a single persistent ``asyncio.Task``.
    """

    def __init__(self, database_url: str, checkpointer_conn_string: str) -> None:
        self._database_url = database_url
        self._checkpointer_conn_string = checkpointer_conn_string
        self._engine: AsyncEngine | None = None
        self._queue: asyncio.Queue[PipelineJob] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._running_jobs: set[asyncio.Task[None]] = set()
        self._started = False
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(_POOL_SIZE)

    async def start(self) -> None:
        """Create the database engine and start the consumer loop.

        Idempotent — safe to call multiple times.
        """
        if self._started:
            _log.warning("Background pipeline worker already started — skipping")
            return
        self._engine = create_async_engine(
            self._database_url,
            pool_size=_POOL_SIZE,
            max_overflow=_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_timeout=_POOL_TIMEOUT,
            connect_args={"ssl": False, "statement_cache_size": 0},
        )
        self._consumer_task = asyncio.create_task(
            self._consumer_loop(),
            name="background-pipeline-worker",
        )
        self._started = True
        _log.info("Background pipeline worker started")

    def submit(self, run_id: uuid.UUID, org_id: uuid.UUID, input_payload: dict[str, Any]) -> None:
        """Submit a pipeline run for background execution.

        Never blocks — pushes to the internal queue immediately.
        """
        if not self._started or (self._consumer_task and self._consumer_task.done()):
            _log.warning("Background worker not available — run %s rejected", run_id)
            return
        job = PipelineJob(run_id=run_id, org_id=org_id, input_payload=input_payload)
        self._queue.put_nowait(job)
        _log.info("Pipeline run %s submitted to background worker", run_id)

    async def stop(self) -> None:
        """Gracefully stop the worker.

        Drains the queue, cancels in-flight jobs after a timeout,
        then disposes the engine.
        """
        self._started = False  # Reject new submissions immediately
        leftover = 0
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
                leftover += 1
                _log.warning("Background worker shutdown: run %s abandoned in queue", job.run_id)
            except asyncio.QueueEmpty:
                break
        if leftover:
            _log.warning("Background worker shutdown: %d queued jobs abandoned", leftover)

        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await asyncio.wait_for(self._consumer_task, timeout=_CONSUMER_STOP_TIMEOUT)
            except (asyncio.CancelledError, TimeoutError):
                _log.warning("Background worker consumer task did not stop cleanly")

        if self._running_jobs:
            _, pending = await asyncio.wait(
                self._running_jobs,
                timeout=_RUNNING_JOBS_DRAIN_TIMEOUT,
                return_when=asyncio.ALL_COMPLETED,
            )
            if pending:
                _log.warning(
                    "Background worker shutdown: %d running jobs did not complete within %.0fs",
                    len(pending),
                    _RUNNING_JOBS_DRAIN_TIMEOUT,
                )
                for t in pending:
                    t.cancel()

        if self._engine is not None:
            await self._engine.dispose()
            _log.info("Background pipeline worker engine disposed")

        _log.info("Background pipeline worker stopped")

    async def _consumer_loop(self) -> None:
        """Consume jobs from the queue and spawn sub-tasks for each."""
        backoff = 0.1
        while True:
            try:
                job = await self._queue.get()
                backoff = 0.1
                task = asyncio.create_task(
                    self._execute_job_with_semaphore(job),
                    name=f"bg-pipeline-run-{job.run_id}",
                )
                self._running_jobs.add(task)
                task.add_done_callback(self._running_jobs.discard)
            except asyncio.CancelledError:
                break
            except Exception:
                _log.exception("Background worker consumer loop error")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def _execute_job(self, job: PipelineJob) -> None:
        """Execute a single pipeline run."""
        _log.info("Background worker executing run %s", job.run_id)
        engine = self._engine
        if engine is None:
            _log.error("Background worker engine not available — run %s will not execute", job.run_id)
            return

        try:
            executor = PipelineExecutor(
                engine,
                checkpointer_conn_string=self._checkpointer_conn_string,
            )
            await executor.execute(
                run_id=job.run_id,
                org_id=job.org_id,
                input_payload=job.input_payload,
            )
            _log.info("Background worker completed run %s", job.run_id)
        except asyncio.CancelledError:
            _log.warning("Background worker run %s cancelled", job.run_id)
        except Exception:
            _log.exception("Background worker failed for run %s", job.run_id)
            try:
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session, session.begin():
                    await set_rls_org(session, job.org_id)
                    await update_run_status(
                        session,
                        job.run_id,
                        "failed",
                        error_code="internal_error",
                    )
            except Exception:
                _log.exception("Background worker failed to mark run %s as failed", job.run_id)

    async def _execute_job_with_semaphore(self, job: PipelineJob) -> None:
        async with self._semaphore:
            await self._execute_job(job)

    def info(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "queue_depth": self._queue.qsize(),
            "in_flight": len(self._running_jobs),
            "semaphore_size": _SEMAPHORE_VALUE,
        }
