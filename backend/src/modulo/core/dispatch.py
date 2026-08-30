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

_shared_redis: AsyncRedis | None = None
_shared_redis_lock = asyncio.Lock()


async def _get_shared_redis() -> AsyncRedis:
    """Return a process-lifetime shared Redis client (created once).

    The dispatch path is hot under webhook load; creating + closing a fresh
    client per enqueue churned connections and accumulated to Upstash's
    connection limit, stalling the SAQ runs worker (2026-08-09). A single
    shared client with the configured pool size is the durable fix.
    """
    global _shared_redis
    if _shared_redis is None:
        async with _shared_redis_lock:
            if _shared_redis is None:
                settings = get_settings()
                _shared_redis = AsyncRedis.from_url(
                    settings.redis_url,
                    socket_keepalive=True,
                    socket_connect_timeout=10,
                    max_connections=settings.saq_redis_pool_size,
                )
    return _shared_redis


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


async def _capacity_deferred(session: AsyncSession, run_id: uuid.UUID) -> bool:
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
        _log.warning(
            "dispatch_run: org run-concurrency check failed for run %s (admitted)",
            run_id,
            exc_info=True,
        )
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

    A successful dispatch also CLEARS ``enqueue_failed_at`` (raw SQL — the
    column ships in a parallel migration): a run that previously failed to
    enqueue and was left ``pending`` with the marker is admitted once its
    retry dispatch lands.
    """
    await session.execute(
        text(
            "UPDATE runs SET dispatcher='saq', saq_job_id=:jid, enqueue_failed_at=NULL, "
            "claim_token = CASE WHEN claim_token IS NULL THEN :tok ELSE claim_token END "
            "WHERE id=:rid"
        ),
        {"rid": run_id, "jid": job_id, "tok": claim_token},
    )


async def _mark_enqueue_failed(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Non-terminal enqueue-failure marker — leave the run pending for retry.

    The old terminal ``_mark_dispatch_failed`` permanently failed every run in a
    >6s Redis outage window with no recovery. Instead the run stays ``pending``
    (``dispatched_at`` already set, ``dispatcher`` NULL) and
    ``enqueue_failed_at=now()`` is stamped (raw SQL — column ships in a parallel
    migration). ``dispatcher_reconcile`` re-dispatches it on a bounded interval
    with a per-tick cap, and terminal-fails it (``dispatch_failed``) only when
    Redis is verifiably reachable AND the marker is older than the TTL backstop.
    """
    await session.execute(
        text("UPDATE runs SET enqueue_failed_at=now() WHERE id=:rid AND status NOT IN ('complete', 'cancelled')"),
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
    *,
    key_suffix: str | None = None,
) -> tuple[str, bool]:
    """Enqueue a run job to SAQ. Returns (job_id, deduped).

    ``key_suffix`` (default empty) makes the SAQ job key ``run:{run_id}:{suffix}``
    instead of the deterministic ``run:{run_id}``. A re-dispatch (reconcile) uses
    a FRESH suffix so SAQ's key-based dedupe never suppresses a re-enqueue of an
    evicted/never-landed job; the atomic claim UPDATE (``claim_run_async``) is the
    real at-most-once dedupe — a second worker claiming the same run loses.

    TOCTOU guard: ``q.job(key)`` is re-checked AFTER the caller's decision and
    immediately before enqueue — if a job now exists under this key (a concurrent
    worker enqueued it in the meantime) the enqueue is skipped and the existing
    deterministic job id returned (``deduped=True``).
    """
    settings = get_settings()
    redis_client = await _get_shared_redis()
    q = RedisQueue(redis_client, name=queue_name)
    function = SAQ_RESUME_RUN_FUNCTION if job_type == "resume_run" else SAQ_EXECUTE_RUN_FUNCTION
    key = f"run:{run_id}" if not key_suffix else f"run:{run_id}:{key_suffix}"
    kwargs: dict[str, Any] = {"run_id": run_id, "org_id": org_id}
    if resume_data:
        kwargs["resume_data"] = resume_data
    # Re-check AFTER the decision and before enqueue — skip if a job now exists.
    if await q.job(key) is not None:
        return q.job_id(key), True
    job = await q.enqueue(
        function,
        key=key,
        timeout=SAQ_RUN_TIMEOUT,
        heartbeat=settings.saq_job_heartbeat,
        retries=settings.saq_run_retries,
        retry_delay=settings.saq_retry_delay,
        retry_backoff=False,
        ttl=SAQ_RUN_TTL,
        **kwargs,
    )
    if job is not None:
        return job.id, False
    # Already enqueued with the same key — deterministic job id.
    return q.job_id(key), True


async def _mark_enqueue_failed_session(run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Non-terminal enqueue-failure marker + webhook-dedup expiry in one session."""
    session = _open_session()
    try:
        async with session.begin():
            from modulo.db.rls import set_rls_execution_context, set_rls_org

            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await _mark_enqueue_failed(session, run_id)
            await _expire_webhook_dedup(session, run_id)
    finally:
        await session.close()


async def _record_saq_job_session(run_id: uuid.UUID, org_id: uuid.UUID, job_id: str) -> None:
    """Record dispatched='saq' + job id + fresh claim token in one session."""
    session = _open_session()
    try:
        async with session.begin():
            from modulo.db.rls import set_rls_execution_context, set_rls_org

            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await _record_saq_job(session, run_id, job_id, _new_claim_token())
    finally:
        await session.close()


async def _enqueue_with_retry(
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    queue_name: str,
    job_type: str,
    resume_data: dict[str, Any] | None,
    key_suffix: str | None,
    fail_fast: bool,
) -> tuple[str, str | None]:
    """Enqueue the run job to SAQ with the configured retry policy.

    On a successful enqueue, records the SAQ job + claim token and returns
    ``('enqueued'|'deduped', job_id)``. On final failure marks the run
    ``enqueue_failed`` (non-terminal, for dispatcher_reconcile recovery) — and
    expires the webhook dedup so a retried webhook is not suppressed — then
    returns ``('enqueue_failed', None)``. The fail-fast (webhook) path skips the
    retry loop.
    """
    try:
        job_id, deduped = await _enqueue_saq(
            str(run_id), str(org_id), queue_name, job_type, resume_data, key_suffix=key_suffix
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if fail_fast:
            _log.exception("dispatch_run: SAQ enqueue failed for run %s (fail-fast)", run_id)
            await _mark_enqueue_failed_session(run_id, org_id)
            return ("enqueue_failed", None)
        _log.warning("dispatch_run: SAQ enqueue failed for run %s: %s", run_id, exc)
        retried = await _retry_enqueue_saq(run_id, org_id, queue_name, job_type, resume_data, key_suffix=key_suffix)
        if retried is None:
            await _mark_enqueue_failed_session(run_id, org_id)
            return ("enqueue_failed", None)
        job_id, deduped = retried
    await _record_saq_job_session(run_id, org_id, job_id)
    return ("deduped" if deduped else "enqueued", job_id)


async def _retry_enqueue_saq(
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    queue_name: str,
    job_type: str,
    resume_data: dict[str, Any] | None,
    *,
    key_suffix: str | None,
) -> tuple[str, bool] | None:
    """Retry the SAQ enqueue a bounded number of times.

    Returns ``(job_id, deduped)`` once an attempt succeeds, or ``None`` when
    every retry failed (the caller then marks the run ``enqueue_failed`` for
    ``dispatcher_reconcile`` recovery). Must not be called on the fail-fast
    (webhook) path.
    """
    for attempt in (1, 2, 3):
        await asyncio.sleep(attempt)
        try:
            return await _enqueue_saq(
                str(run_id), str(org_id), queue_name, job_type, resume_data, key_suffix=key_suffix
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc2:
            _log.warning(
                "dispatch_run: SAQ enqueue retry %d failed for run %s: %s",
                attempt,
                run_id,
                exc2,
            )
    return None


async def dispatch_run(
    run_id: str,
    org_id: str,
    *,
    queue: str = "runs",
    job_type: str = "execute_run",
    resume_data: dict[str, Any] | None = None,
    fail_fast: bool = False,
    key_suffix: str | None = None,
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
      * ``('enqueue_failed', None)`` — final enqueue failure after all retries.
        The run is LEFT ``pending`` (non-terminal): ``dispatched_at`` stays set,
        ``dispatcher`` stays NULL and ``enqueue_failed_at=now()`` is stamped so
        ``dispatcher_reconcile`` re-dispatches it on a bounded interval (and
        terminal-fails it with ``dispatch_failed`` only once Redis is verifiably
        reachable and the marker is older than the TTL backstop). The caller
        records an ``error_event`` (source='saq', function='webhook_dispatch').

    ``key_suffix`` (default empty) makes the SAQ job key ``run:{run_id}:{suffix}``;
    ``dispatcher_reconcile`` re-dispatches with a FRESH suffix so SAQ key dedupe
    never suppresses the recovery enqueue.
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
            from modulo.db.crud.run import get_run
            from modulo.db.models.run import TERMINAL_STATUSES
            from modulo.db.rls import set_rls_execution_context, set_rls_org

            await set_rls_org(session, oid)
            await set_rls_execution_context(session)
            run = await get_run(session, rid)
            if run is not None and run.status in TERMINAL_STATUSES:
                # A terminal run must NEVER be enqueued for execution — the
                # executor would resurrect it to ``running``. Guardrail-blocked
                # runs reach eval_failed at creation (FAR-208 item 2) and are
                # refused here; this also hardens resumes against terminal runs.
                _log.info("dispatch_run: run %s already terminal (%s) — not dispatched", rid, run.status)
                return ("terminal_skipped", None)
            if await _capacity_deferred(session, rid):
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
            from modulo.db.rls import set_rls_execution_context, set_rls_org

            await set_rls_org(session, oid)
            await set_rls_execution_context(session)
            await _record_dispatched(session, rid)
    finally:
        await session.close()

    return await _enqueue_with_retry(rid, oid, queue_name, job_type, resume_data, key_suffix, fail_fast)
