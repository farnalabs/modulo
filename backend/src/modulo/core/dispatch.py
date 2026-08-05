"""Dispatch pipeline runs to SAQ — the only dispatch path post-cutover.

Covers enqueue and SAQ job dedup for the ``execute_run``/``resume_run`` job
functions via :func:`dispatch_run` (with capacity gating).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Any, cast

from redis.asyncio import Redis as AsyncRedis
from saq.queue.redis import RedisQueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# SAQ job function names — registered in core/saq_worker.py.
SAQ_EXECUTE_RUN_FUNCTION = "modulo.core.saq_worker.execute_run"
SAQ_RESUME_RUN_FUNCTION = "modulo.core.saq_worker.resume_run"

# Per-job knobs (plan F5): ttl=300 is finish-origin (SPIKE-verified), so a
# 300s ttl after completion is safe; timeout=7200 covers long agent runs.
SAQ_RUN_TIMEOUT = 7200
SAQ_RUN_TTL = 300


def _open_session() -> AsyncSession:
    # Reuse the shared, tuned app engine (pool_pre_ping, asyncpg statement cache
    # disabled for Fly/HAProxy, pooled sizing) rather than a divergent second pool.
    from modulo.api.dependencies import get_or_create_engine

    return async_sessionmaker(
        get_or_create_engine(get_settings()),
        expire_on_commit=False,
        autobegin=False,
    )()


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
    job_type: str = "execute_run",
    resume_data: dict[str, Any] | None = None,
    fail_fast: bool = False,
) -> tuple[str, str | None]:
    """Route a run to SAQ (the only dispatch path post-cutover).

    Returns ``(outcome, job_id)``:

      * ``('enqueued', job_id)``  — job is on the SAQ queue.
      * ``('deduped', job_id)``   — a SAQ job with the same key already exists.
      * ``('deferred', None)``    — capacity-blocked (no enqueue, no dispatched_at)
        or enqueue failed.
    """
    settings = get_settings()
    rid = uuid.UUID(str(run_id))
    oid = uuid.UUID(str(org_id))
    queue_name = queue or settings.saq_runs_queue

    # Capacity check FIRST (plan F3b/F3e). The run itself is excluded from the
    # count so a resume never counts against its own slot; a capacity-deferred
    # resume is re-dispatched by dispatcher_reconcile. No dispatched_at here.
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

    # Write dispatched_at BEFORE enqueue (F3e).
    session = _open_session()
    try:
        async with session.begin():
            from modulo.db.rls import set_rls_org

            await set_rls_org(session, oid)
            await _record_dispatched(session, rid)
    finally:
        await session.close()

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


def dispatch_run_sync(run_id: str, org_id: str, **kwargs: Any) -> tuple[str, str | None]:
    """Sync facade for sync call sites (webhook handlers)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(dispatch_run(run_id, org_id, **kwargs))
    # A loop is already running in this thread — run in a separate thread.
    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(dispatch_run(run_id, org_id, **kwargs))
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return cast(tuple[str, str | None], result["value"])
