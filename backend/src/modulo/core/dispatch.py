"""Dispatch pipeline runs to SAQ — the only dispatch path post-cutover.

Covers enqueue and SAQ job dedup for the ``execute_run``/``resume_run`` job
functions via :func:`dispatch_run` (with capacity gating).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from saq.queue.redis import RedisQueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# SAQ job function names — registered in core/saq_worker.py.
SAQ_EXECUTE_RUN_FUNCTION = "modulo.core.saq_worker.execute_run"
SAQ_RESUME_RUN_FUNCTION = "modulo.core.saq_worker.resume_run"

# Per-job knobs (plan F5): ttl=300 is FINISH-origin — verified against the
# pinned saq==0.26.4 source: saq/queue/redis.py:436-441 (_finish applies
# ``setex(job_id, job.ttl, ...)`` ONLY when the job completes) and
# saq/queue/redis.py:447-471 (_enqueue stores the job hash with a plain SET and
# no TTL). A 300s ttl therefore never expires a mid-run job hash (timeout=7200
# covers long agent runs); it only bounds how long a COMPLETED job is retained.
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


async def _org_capacity_deferred(
    session: AsyncSession,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    job_type: str = "execute_run",
) -> bool:
    """True when the org is at its ``run_concurrency_limit`` (dispatch admission).

    Org-level admission control: a run whose org has ``run_concurrency_limit``
    configured and already has that many executing/claimed runs is deferred
    (returned to ``pending``) instead of enqueued — the shared worker pool is
    global, so a single org must not flood it across all its pipelines.

    A ``resume_run`` dispatch is NEVER org-cap deferred: a resume is the
    continuation of an ALREADY-ADMITTED run — the run is already ``running``
    and already consumes an org slot — so the org-cap gate (which exists to
    gate NEW run admissions) must not re-defer it. Deferring a resume would
    return ``("deferred", None)`` to ``recover_node`` (HTTP 500) and lose the
    ``resume_data`` when ``dispatcher_reconcile`` later re-dispatches it as
    ``execute_run`` with empty resume data.

    Fail-open, loud: any error reading the org limit or counting active runs
    logs a warning and ADMITS the run (treats it as no-cap), matching the
    executor's capacity philosophy. When the cap is hit the run is deferred
    and — ONLY for a currently-``pending`` run — demoted with the
    ``org_capacity_limited`` reason marker so it is treated as
    stranded-capacity (re-dispatch, never ``never_dispatched``).

    Re-dispatch ownership (FAR-108): ``dispatcher_reconcile`` re-dispatches a
    capacity-marked pending run whose heartbeat is stale or NULL — the
    ``CAPACITY_REDISPATCH_SECONDS`` (~120s) carve-out in
    ``cron_helpers._reconcile_capacity_marker_exclusion``, the fast path that
    used to wait for the multi-minute stale-run sweep. The re-dispatch is
    gated atomically by ``dispatch_run`` re-checking capacity, so a
    still-blocked run is re-deferred (counted ``capacity_deferred``, never
    alerted). The heartbeat gate throttles the sandbox-cap claim→demote churn
    loop to one attempt per redispatch window — this is why a FRESH-heartbeat
    row is NOT re-dispatched on every 60s pass; ``stale_run_recovery_sweep``
    remains its single re-dispatch owner when it strands past the TTL.

    A run that is NOT currently ``pending`` (``running``/``awaiting_human``/
    ``claimed`` — e.g. a node recovery or a committed HITL decision being
    resumed as ``resume_run``) is deferred WITHOUT writing status, mirroring
    :func:`_capacity_deferred`. Demoting those unconditionally would silently
    drop the resume payload / committed gate decision: the run would pick up
    the ``org_capacity_limited`` marker and be re-dispatched as ``execute_run``
    with empty ``resume_data``, re-interrupting or re-running the failed node.
    Leaving its status untouched preserves the caller's resume intent; the
    next ``dispatcher_reconcile`` pass re-dispatches it correctly as
    ``resume_run`` once a slot frees.
    """
    if job_type == "resume_run":
        return False

    from modulo.db.crud.run import (
        ERROR_CODE_ORG_CAPACITY_LIMITED,
        count_active_runs_for_org,
        get_org_run_concurrency_limit,
        get_run,
        update_run_status,
    )

    try:
        limit = await get_org_run_concurrency_limit(session, org_id)
        if limit is None:
            return False
        active = await count_active_runs_for_org(session, org_id, include_pending=False, exclude_run_id=run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("dispatch_run: org run-concurrency check failed for run %s (admitted)", run_id)
        return False
    if active < limit:
        return False
    _log.info(
        "dispatch_run: run %s org-capacity-deferred (%d active, limit %d)",
        run_id,
        active,
        limit,
    )
    run = await get_run(session, run_id)
    if run is not None and run.status == "pending":
        await update_run_status(session, run_id, "pending", error_code=ERROR_CODE_ORG_CAPACITY_LIMITED)
    return True


async def _record_dispatched(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Write dispatched_at BEFORE enqueue (F3e)."""
    await session.execute(
        text("UPDATE runs SET dispatched_at=now() WHERE id=:rid"),
        {"rid": run_id},
    )


async def _record_saq_job(session: AsyncSession, run_id: uuid.UUID, job_id: str, claim_token: str) -> None:
    """Record dispatcher='saq' + job id + a fresh claim token after a successful SAQ enqueue.

    The claim token is only written when the run has NOT been claimed yet: the
    worker can dequeue the job and ``claim_run_async`` (which atomically rotates
    ``runs.claim_token`` to its own value) between the enqueue and this write.
    Overwriting it here would clobber the worker's token, so the worker's next
    heartbeat would raise ``ClaimSupersededError`` and the active executor would
    abort. Once a worker claims the run, the worker owns the claim token — the
    dispatcher must not touch it.
    """
    await session.execute(
        text(
            "UPDATE runs SET dispatcher='saq', saq_job_id=:jid, "
            "claim_token = CASE WHEN claim_token IS NULL THEN :tok ELSE claim_token END "
            "WHERE id=:rid"
        ),
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
        max_connections=settings.saq_redis_pool_size,
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

      * ``('enqueued', job_id)``     — job is on the SAQ queue.
      * ``('deduped', job_id)``      — a SAQ job with the same key already exists.
      * ``('deferred', None)``       — capacity-blocked (no enqueue, no
        dispatched_at). Either the run's pipeline is at
        ``max_concurrent_runs`` or — NEW org-level admission control — the
        org is at its ``run_concurrency_limit``. A currently-``pending`` run
        is also demoted with the ``org_capacity_limited`` reason marker so the
        stale-run sweep recovers it as stranded-capacity; a non-pending run
        (``running``/``awaiting_human``/``claimed`` resume) is deferred without
        a status write so its resume payload / committed HITL decision is
        preserved.
      * ``('enqueue_failed', None)`` — fail-fast enqueue failure (webhook path);
        the caller records an ``error_event`` (source='saq',
        function='webhook_dispatch'). Only produced when ``fail_fast=True``.
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
            if await _org_capacity_deferred(session, rid, oid, job_type=job_type):
                _log.info("dispatch_run: run %s org-capacity-deferred (no enqueue)", rid)
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
            return ("enqueue_failed", None)
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
