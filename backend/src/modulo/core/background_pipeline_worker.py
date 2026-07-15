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
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.run import update_run_status
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)


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
        self._stopping = False

    async def start(self) -> None:
        """Create the database engine and start the consumer loop."""
        self._engine = create_async_engine(
            self._database_url,
            pool_size=3,
            max_overflow=6,
            pool_pre_ping=True,
        )
        self._consumer_task = asyncio.create_task(
            self._consumer_loop(),
            name="background-pipeline-worker",
        )
        _log.info("Background pipeline worker started")

    def submit(self, run_id: uuid.UUID, org_id: uuid.UUID, input_payload: dict[str, Any]) -> None:
        """Submit a pipeline run for background execution.

        Never blocks — pushes to the internal queue immediately.
        """
        job = PipelineJob(run_id=run_id, org_id=org_id, input_payload=input_payload)
        self._queue.put_nowait(job)
        _log.info("Pipeline run %s submitted to background worker", run_id)

    async def stop(self) -> None:
        """Gracefully stop the worker.

        Cancels the consumer task first (no new jobs accepted), then waits
        for in-flight jobs to complete (with a 30s timeout), then disposes
        the engine.
        """
        self._stopping = True
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._consumer_task, timeout=5.0)

        if self._running_jobs:
            _, pending = await asyncio.wait(
                self._running_jobs,
                timeout=30.0,
                return_when=asyncio.ALL_COMPLETED,
            )
            if pending:
                _log.warning(
                    "Background worker shutdown: %d running jobs did not complete within 30s",
                    len(pending),
                )
                for t in pending:
                    t.cancel()

        if self._engine is not None:
            await self._engine.dispose()
            _log.info("Background pipeline worker engine disposed")

        _log.info("Background pipeline worker stopped")

    async def _consumer_loop(self) -> None:
        """Consume jobs from the queue and spawn sub-tasks for each."""
        while True:
            try:
                job = await self._queue.get()
                task = asyncio.create_task(
                    self._execute_job(job),
                    name=f"bg-pipeline-run-{job.run_id}",
                )
                self._running_jobs.add(task)
                task.add_done_callback(self._running_jobs.discard)
            except asyncio.CancelledError:
                break
            except Exception:
                _log.exception("Background worker consumer loop error")

    async def _execute_job(self, job: PipelineJob) -> None:
        """Execute a single pipeline run."""
        _log.info("Background worker executing run %s", job.run_id)
        try:
            assert self._engine is not None
            executor = PipelineExecutor(
                self._engine,
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
                assert self._engine is not None
                factory = async_sessionmaker(self._engine, expire_on_commit=False)
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
