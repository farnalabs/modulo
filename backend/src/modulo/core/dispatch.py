"""dispatch_run — the single gating point for run dispatch.

Every run dispatch flows through :func:`dispatch_run` (plan F3e). In SAQ mode
(``SAQ_ENABLED=true``) it enqueues ``execute_run`` / ``resume_run`` jobs to the
SAQ runs queue with per-job knobs from Settings and records the ``dispatcher``
column. In shadow mode (``SAQ_ENABLED=false``) ``execute_run`` routes to Celery
via the existing Celery dispatch (``dispatcher`` stays NULL) while ``resume_run``
runs IN-PROCESS (no enqueue — there is no Celery resume path and the SAQ worker
wiring lands in a later PR slice, so queueing a resume in shadow would silently
drop the recovery).

The dispatcher column reflects WHERE THE JOB ACTUALLY WENT:
``'saq'`` iff enqueued to SAQ; NULL iff routed to Celery.

Capacity gating (plan F3b/F3e) applies to new ``execute_run`` dispatches only —
a run at the pipeline's ``max_concurrent_runs`` is returned ``'deferred'`` with
NO enqueue and NO ``dispatched_at`` (dispatcher_reconcile re-dispatches when
capacity frees). ``resume_run`` skips the gate because the run already holds a
slot, and the shadow/Celery path keeps today's executor-side capacity gate so
shadow behaviour is unchanged.

On enqueue failure: webhook handlers pass ``fail_fast=True`` (respond 202,
leave recovery to ``dispatcher_reconcile`` — never block the request on
backoff); elsewhere ``fail_fast=False`` retries with backoff and on final
failure marks the run ``dispatch_failed`` and expires the webhook dedup hash.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from saq.queue.redis import RedisQueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# SAQ job function names — registered in core/saq_worker.py (PR B step 5).
SAQ_EXECUTE_RUN_FUNCTION = "modulo.core.saq_worker.execute_run"
SAQ_RESUME_RUN_FUNCTION = "modulo.core.saq_worker.resume_run"

# Per-job knobs (plan F5): ttl=300 is finish-origin (SPIKE-verified), so a
# 300s ttl after completion is safe; timeout=7200 covers long agent runs.
SAQ_RUN_TIMEOUT = 7200
SAQ_RUN_TTL = 300

_ENGINE = None


def _get_engine() -> Any:
    global _ENGINE
    if _ENGINE is None:
        settings = get_settings()
        kw: dict[str, Any] = {"url": settings.database_url}
        if settings.modulo_db.lower() == "postgres":
            kw["connect_args"] = {"timeout": 10, "ssl": False}
        _ENGINE = create_async_engine(**kw)
    return _ENGINE


def _open_session() -> AsyncSession:
    return async_sessionmaker(_get_engine(), expire_on_commit=False, autobegin=False)()


def _new_claim_token() -> str:
    """DISTINCT per-claim token — never identical to the deterministic SAQ job id."""
    return uuid.uuid4().hex


async def _capacity_deferred(session: AsyncSession, run_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    """True when the run's pipeline is at ``max_concurrent_runs`` (plan F3b)."""
    from modulo.db.crud.run import count_active_runs_for_pipeline, get_run
    from modulo.db.models.pipeline import Pipeline

    run = await get_run(session, run_id)
    if run is None:
        _log.warning("dispatch_run: run %s not found for capacity check", run_id)
        return True
    pipeline = await session.get(Pipeline, run.pipeline_id)
    if pipeline is None:
        return True
    max_concurrent = pipeline.max_concurrent_runs
    if max_concurrent <= 0:
        return False
    active = await count_active_runs_for_pipeline(
        session, run.pipeline_id, include_pending=False, exclude_run_id=run_id
    )
    return active >= max_concurrent


async def _record_dispatched(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Write dispatched_at BEFORE enqueue (F3e)."""
    await session.execute(
        text("UPDATE runs SET dispatched_at=now() WHERE id=:rid"),
        {"rid": run_id},
    )


async def _record_saq_job(session: AsyncSession, run_id: uuid.UUID, job_id: str, claim_token: str) -> None:
    """Record dispatcher='saq' + job id + fresh claim token after a successful SAQ enqueue."""
    await session.execute(
        text("UPDATE runs SET dispatcher='saq', saq_job_id=:jid, claim_token=:tok WHERE id=:rid"),
        {"rid": run_id, "jid": job_id, "tok": claim_token},
    )


async def _mark_dispatch_failed(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Final enqueue failure — mark the run failed with error_code='dispatch_failed'."""
    await session.execute(
        text(
            "UPDATE runs SET status='failed', error_code='dispatch_failed', completed_at=now() "
            "WHERE id=:rid AND status NOT IN ('complete', 'cancelled')"
        ),
        {"rid": run_id},
    )


async def _expire_webhook_dedup(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Expire the webhook dedup hash for this run so a retried webhook is not suppressed."""
    from sqlalchemy import delete, select

    from modulo.db.models.trigger_event import TriggerEvent
    from modulo.db.models.webhook import WebhookDedupHash

    ev = await session.execute(
        select(TriggerEvent.trigger_id, TriggerEvent.raw_payload_hash)
        .where(TriggerEvent.run_id == run_id)
        .order_by(TriggerEvent.received_at.desc())
        .limit(1)
    )
    row = ev.first()
    if row is None:
        return
    await session.execute(
        delete(WebhookDedupHash).where(
            WebhookDedupHash.trigger_id == row[0],
            WebhookDedupHash.payload_hash == row[1],
        )
    )


def _pg_conn_string(database_url: str) -> str:
    """Strip the SQLAlchemy prefix for a psycopg-compatible checkpointer URL."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


async def _resume_inline(
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    resume_data: dict[str, Any] | None,
) -> None:
    """Resume a run in-process (shadow mode) — no queue, no worker.

    PR B-1 lands the dispatch routing BEFORE the SAQ worker wiring, so in
    shadow mode (``SAQ_ENABLED=false``) a node recovery must NOT be enqueued to
    a queue no worker consumes. Replay the pre-B-1 ``recover_run_node`` path
    (``PipelineExecutor.resume``) directly so recovery can never silently drop.
    """
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    engine = _get_engine()
    executor = PipelineExecutor(engine, checkpointer_conn_string=_pg_conn_string(str(engine.url)))
    await executor.resume(run_id=run_id, org_id=org_id, resume_data=resume_data or {})


async def _enqueue_saq(
    run_id: str,
    org_id: str,
    queue_name: str,
    job_type: str,
    resume_data: dict[str, Any] | None,
) -> tuple[str, bool]:
    """Enqueue a run job to SAQ. Returns (job_id, deduped)."""
    settings = get_settings()
    redis_client = AsyncRedis.from_url(
        settings.redis_url,
        socket_keepalive=True,
        socket_connect_timeout=10,
    )
    try:
        q = RedisQueue(redis_client, name=queue_name)
        function = SAQ_RESUME_RUN_FUNCTION if job_type == "resume_run" else SAQ_EXECUTE_RUN_FUNCTION
        kwargs: dict[str, Any] = {"run_id": run_id, "org_id": org_id}
        if resume_data:
            kwargs["resume_data"] = resume_data
        job = await q.enqueue(
            function,
            key=f"run:{run_id}",
            timeout=SAQ_RUN_TIMEOUT,
            heartbeat=settings.saq_job_heartbeat,
            retries=settings.saq_run_retries,
            retry_delay=settings.saq_retry_delay,
            retry_backoff=False,
            ttl=SAQ_RUN_TTL,
            **kwargs,
        )
    finally:
        await redis_client.aclose()
    if job is not None:
        return job.id, False
    # Already enqueued with the same key — deterministic job id.
    return q.job_id(f"run:{run_id}"), True


async def dispatch_run(
    run_id: str,
    org_id: str,
    *,
    queue: str = "runs",
    celery_queue: str = "runs_automated",
    job_type: str = "execute_run",
    resume_data: dict[str, Any] | None = None,
    fail_fast: bool = False,
) -> tuple[str, str | None]:
    """Route a run to SAQ (or Celery in shadow mode).

    Returns ``(outcome, job_id)``:

      * ``('enqueued', job_id)``  — job is on the SAQ queue (or sent to Celery).
      * ``('deduped', job_id)``   — a SAQ job with the same key already exists.
      * ``('resumed', None)``     — shadow-mode ``resume_run`` executed in-process
        (no enqueue).
      * ``('deferred', None)``    — capacity-blocked (no enqueue, no dispatched_at)
        or enqueue failed.
    """
    settings = get_settings()
    rid = uuid.UUID(str(run_id))
    oid = uuid.UUID(str(org_id))
    use_saq = bool(settings.saq_enabled)
    queue_name = queue or settings.saq_runs_queue

    # Shadow-mode resume: the SAQ worker is not wired in this slice and there is
    # no Celery resume path, so enqueuing a resume would silently drop the
    # recovery (the run would stay stuck). Resume inline in-process — exactly the
    # pre-B-1 recover_run_node behaviour — and never touch dispatched_at or SAQ.
    if not use_saq and job_type == "resume_run":
        await _resume_inline(rid, oid, resume_data)
        return ("resumed", None)

    # Capacity check FIRST (plan F3b/F3e). The run itself is excluded from the
    # count so a resume never counts against its own slot; a capacity-deferred
    # resume is re-dispatched by dispatcher_reconcile. No dispatched_at here.
    if use_saq:
        session = _open_session()
        try:
            async with session.begin():
                from modulo.db.rls import set_rls_org

                await set_rls_org(session, oid)
                if await _capacity_deferred(session, rid, oid):
                    _log.info("dispatch_run: run %s capacity-deferred (no enqueue)", rid)
                    return ("deferred", None)
        finally:
            await session.close()

    # Write dispatched_at BEFORE enqueue.
    session = _open_session()
    try:
        async with session.begin():
            from modulo.db.rls import set_rls_org

            await set_rls_org(session, oid)
            await _record_dispatched(session, rid)
    finally:
        await session.close()

    if use_saq:
        try:
            job_id, deduped = await _enqueue_saq(str(rid), str(oid), queue_name, job_type, resume_data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if fail_fast:
                _log.exception("dispatch_run: SAQ enqueue failed for run %s (fail-fast)", rid)
                return ("deferred", None)
            _log.warning("dispatch_run: SAQ enqueue failed for run %s: %s", rid, exc)
            for attempt in (1, 2, 3):
                await asyncio.sleep(attempt)
                try:
                    job_id, deduped = await _enqueue_saq(str(rid), str(oid), queue_name, job_type, resume_data)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc2:
                    _log.warning(
                        "dispatch_run: SAQ enqueue retry %d failed for run %s: %s",
                        attempt,
                        rid,
                        exc2,
                    )
            else:
                session = _open_session()
                try:
                    async with session.begin():
                        from modulo.db.rls import set_rls_org

                        await set_rls_org(session, oid)
                        await _mark_dispatch_failed(session, rid)
                        await _expire_webhook_dedup(session, rid)
                finally:
                    await session.close()
                return ("deferred", None)

        session = _open_session()
        try:
            async with session.begin():
                from modulo.db.rls import set_rls_org

                await set_rls_org(session, oid)
                await _record_saq_job(session, rid, job_id, _new_claim_token())
        finally:
            await session.close()
        return ("deduped" if deduped else "enqueued", job_id)

    # Shadow mode — route execute_run to Celery via the existing dispatch;
    # dispatcher stays NULL.
    from modulo.core.pipeline_executor_task import dispatch as celery_dispatch

    try:
        celery_dispatch(str(rid), str(oid), celery_queue)
    except Exception as exc:
        if fail_fast:
            _log.exception("dispatch_run: Celery dispatch failed for run %s (fail-fast)", rid)
            return ("deferred", None)
        raise exc from None
    return ("enqueued", None)


def dispatch_run_sync(run_id: str, org_id: str, **kwargs: Any) -> tuple[str, str | None]:
    """Sync facade for sync Celery task contexts (CronFireTask/PollingFireTask)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(dispatch_run(run_id, org_id, **kwargs))
    # A loop is already running in this thread — run in a separate thread.
    result: dict[str, tuple[str, str | None]] = {}

    def _runner() -> None:
        result["value"] = asyncio.run(dispatch_run(run_id, org_id, **kwargs))

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    return result["value"]
