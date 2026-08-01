"""DEPRECATED — replaced by Celery ExecuteRunTask (2026-07).

Application-level background worker for pipeline execution.

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

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.run import cancel_run, update_run_status
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

    Creates a fresh database engine per job to avoid sharing engine state
    across concurrent background executions. Submissions go to an
    ``asyncio.Queue`` and are consumed by a single persistent ``asyncio.Task``.
    """

    def __init__(self, database_url: str, checkpointer_conn_string: str) -> None:
        self._database_url = database_url
        self._checkpointer_conn_string = checkpointer_conn_string
        self._queue: asyncio.Queue[PipelineJob] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._running_jobs: set[asyncio.Task[None]] = set()
        self._started = False
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(_POOL_SIZE)

    async def start(self) -> None:
        """Start the consumer loop.

        Idempotent — safe to call multiple times.
        """
        if self._started:
            _log.warning("Background pipeline worker already started — skipping")
            return
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

        Drains the queue, cancels in-flight jobs after a timeout.
        Each running job disposes its own engine upon completion.
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

        _log.info("Background pipeline worker stopped")

    async def cleanup_stale_runs(self) -> int:
        """Kill runs stuck in pending/running for longer than their pipeline's configured timeout.

        Uses each pipeline's ``stale_run_timeout_minutes`` (defaults to 30 at the DB level
        since migration 0029_fix_stale_run_timeout_non_null, so the column is never null).
        Called automatically at the start of each consumer loop iteration.

        Iterates over all orgs (``organisations`` has no RLS) so the cleanup
        catches runs across every tenant despite strict RLS on the ``runs`` table.
        """
        from sqlalchemy import text

        engine = create_async_engine(
            self._database_url,
            pool_size=1,
            max_overflow=2,
            pool_pre_ping=True,
            pool_timeout=10,
            connect_args={"ssl": False, "statement_cache_size": 0},
        )
        # Assumes the schema is migrated (stale_run_timeout_minutes is NOT NULL). Against
        # an unmigrated schema the NULL value makes `NULL * INTERVAL '1 minute'` yield NULL
        # and the stale-run cleanup silently no-ops. Entrypoint runs migrations before the
        # worker starts, so this is safe in production.
        killed = 0
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            org_ids: list[uuid.UUID] = []
            async with factory() as session, session.begin():
                org_result = await session.execute(text("SELECT id FROM organisations"))
                org_ids = [row[0] for row in org_result]

            for org_id in org_ids:
                try:
                    async with factory() as session, session.begin():
                        await set_rls_org(session, org_id)
                        rows = await session.execute(
                            text("""
                                SELECT r.id, r.status, p.stale_run_timeout_minutes
                                FROM runs r
                                JOIN pipelines p ON p.id = r.pipeline_id
                                WHERE r.status IN ('running', 'pending')
                                  AND r.updated_at < NOW()
                                    - p.stale_run_timeout_minutes * INTERVAL '1 minute'
                                  AND (r.outputs_json IS NULL OR r.outputs_json::jsonb = '{}'::jsonb)
                                  AND r.claimed_by IS NOT NULL
                            """)
                        )
                        for row in rows:
                            run_id = row[0]
                            status = row[1]
                            timeout = row[2]
                            await cancel_run(session, run_id, error_code="stale_run_killed")
                            _log.warning(
                                "Killed stale run %s (%s) \u2014 stuck >%s min with no node progress",
                                run_id,
                                status,
                                timeout,
                            )
                            killed += 1
                except Exception:
                    _log.exception("cleanup_stale_runs failed for org %s", org_id)
        except Exception:
            _log.exception("cleanup_stale_runs failed")
        finally:
            await engine.dispose()
        if killed:
            _log.info("cleanup_stale_runs: killed %d stale run(s)", killed)
        return killed

    async def _consumer_loop(self) -> None:
        """Consume jobs from the queue and spawn sub-tasks for each."""
        backoff = 0.1
        while True:
            try:
                await self.cleanup_stale_runs()
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
        engine = create_async_engine(
            self._database_url,
            pool_size=2,
            max_overflow=4,
            pool_pre_ping=True,
            pool_timeout=10,
            connect_args={"ssl": False, "statement_cache_size": 0},
        )
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
        finally:
            await engine.dispose()

    async def _execute_job_with_semaphore(self, job: PipelineJob) -> None:
        async with self._semaphore:
            await self._execute_job(job)

    @property
    def is_alive(self) -> bool:
        """Check whether the consumer loop is running.

        Returns True if the worker has been started and its consumer
        task is still active (not cancelled or finished).
        """
        return self._started and self._consumer_task is not None and not self._consumer_task.done()

    def info(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "queue_depth": self._queue.qsize(),
            "in_flight": len(self._running_jobs),
            "semaphore_size": _SEMAPHORE_VALUE,
        }
