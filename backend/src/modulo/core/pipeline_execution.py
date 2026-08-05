"""Pipeline execution core for SAQ (PR C of the Celery->SAQ migration).

This module is the single home for the claim / execute / heartbeat / complete /
stale-sweep logic that was historically embedded in
:mod:`modulo.core.pipeline_executor_task` (the Celery task module deleted by
PR C of the Celery->SAQ migration). The SAQ workers delegate here.

NOT here: ``dispatch_run``, cron firing, fire/report jobs.

Engine injection: every entry point takes its engine(s) explicitly so the
async execution path passes its own async engine. No module-level engine globals.

Staleness constants (plan F4 / F1 ordering):

    RUN_CLAIM_STALE_SECONDS = 450  SAQ runs only
    SAQ_JOB_HEARTBEAT       = 300  SAQ job heartbeat knob
    RUN_HEARTBEAT_SECONDS   = 30   DB heartbeat cadence

Staleness values are configurable via settings (see the F4 Settings section in
:mod:`modulo.settings`). The legacy sweep windows default to
never_dispatched=300 / worker_lost=600 (today's beat-sweep values, 5 and 10
minutes) and stay decoupled from ``RUN_CLAIM_STALE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.run import get_run

_log = logging.getLogger(__name__)

# Claim staleness gates (configurable via settings).
RUN_CLAIM_STALE_SECONDS = 450

# DB heartbeat cadence (F4). Must stay well below the 300s SAQ sweep threshold.
RUN_HEARTBEAT_SECONDS = 30
SAQ_JOB_HEARTBEAT = 300

# Terminal "success" status written by _mark_complete — MUST match the runs
# status CHECK constraint ('complete', NOT 'completed'). See
# db/models/run.py:ck_runs_status.
RUN_COMPLETE_STATUS = "complete"

# Durable backstop for capacity-blocked pending runs. Sized to exceed the
# worst-case queue wait: (max_concurrent - 1) * node timeout, with margin for
# the 120->600s exponential retry backoff plus worker restarts.
CAPACITY_TIMEOUT_TTL_MINUTES = 120

# Re-dispatch TTL for stranded capacity-blocked runs. A run demoted to
# ``pending`` with a capacity marker is normally retried in-process by
# ``PipelineExecutor._retry_pending``, which refreshes ``heartbeat_at`` on each
# attempt. If the worker process hosting that loop dies (deploy/crash/restart)
# the run would otherwise sit ``pending`` until the 120-min capacity_timeout
# sweep TERMINAL-FAILS a legitimate never-executed run. This window re-dispatches
# a stranded run long before that — but ONLY when its heartbeat is stale (the
# in-process loop is provably gone), and never once it is already past the
# capacity_timeout TTL (those must fail, not be resurrected forever).
#
# Sized ABOVE the retry loop's worst-case backoff sleep (600s) plus the 15s
# poll interval, so a LIVE loop's per-attempt heartbeat refresh never trips the
# fence (a 10-minute TTL would race the 600s backoff and risk double-retry
# loops). A genuinely stranded run gets ~10 re-dispatch attempts (120/12) before
# the capacity_timeout backstop fails it.
_STRANDED_REDISPATCH_TTL_MINUTES = 12

_DEFAULT_CLAIM_CAP = 5

# SAQ run claim cap (plan F6a) — distinct per-claim value; SAQ retries reuse the
# same saq_job_id, so the cap bounds re-claims on an at-most-once boundary.
SAQ_RUN_CLAIM_CAP = 20


def get_settings() -> Any:
    from modulo.settings import get_settings as _get_settings

    return _get_settings()


class SchedulerDBError(Exception):
    """Raised when a scheduler DB query fails transiently.

    Relocated from ``modulo.core.pipeline_executor_task`` (PR B-2, plan F1) so
    the SAQ scheduler modules never import the deleted Celery task module.
    """

    pass


def _make_sync_url(database_url: str) -> str:
    """Convert async DB URL to sync by replacing async driver with sync equivalent."""
    return (
        database_url.replace("+asyncpg", "+psycopg").replace("+aiomysql", "+mysqldb").replace("+aiosqlite", "+pysqlite")
    )


def _resolve_claim_stale_seconds(*, stale_seconds: int | None) -> int:
    """Resolve the claim staleness window.

    Uses ``RUN_CLAIM_STALE_SECONDS`` (450), configurable via settings.
    An explicit ``stale_seconds`` overrides settings (used by tests and by the
    SAQ reconcile path later).
    """
    if stale_seconds is not None:
        return stale_seconds
    return int(get_settings().run_claim_stale_seconds)


def build_claim_update(*, stale_seconds: int, claim_cap: int = _DEFAULT_CLAIM_CAP) -> Any:
    """Build the atomic claim UPDATE for a pipeline run.

    The statement is a single ``UPDATE ... WHERE ... RETURNING id``: exactly one
    concurrent claimer wins because the row transitions out of the claimable
    state in the same statement that claims it (no check-then-act window).

    Claimable rows:
      * ``status = 'pending'`` always.
      * ``status = 'running'`` when the heartbeat is older than *stale_seconds*.

    ``claim_cap`` bounds the number of claims (claim_count) per run.

    Callers pass the full parameter dict (rid / oid / stale_seconds / claim_cap)
    at execute time.
    """
    return text(
        "UPDATE runs SET status='running', heartbeat_at=now(), claim_count=claim_count+1 "
        "WHERE id=:rid AND organisation_id=:oid "
        "AND (status = 'pending' "
        "     OR (status = 'running' AND heartbeat_at < now() - (:stale_seconds * interval '1 second'))) "
        "AND claim_count < :claim_cap "
        "RETURNING id"
    )


def _claim_params(run_id: str, org_id: str, stale_seconds: int, claim_cap: int) -> dict[str, object]:
    return {"rid": run_id, "oid": org_id, "stale_seconds": stale_seconds, "claim_cap": claim_cap}


async def claim_run_async(
    aengine: AsyncEngine,
    run_id: str,
    org_id: str,
    stale_seconds: int | None = None,
    *,
    claim_cap: int = _DEFAULT_CLAIM_CAP,
) -> bool:
    """Claim a pending or stale-running run via an atomic SQL update (async).

    Used by the SAQ execute path.
    """
    window = _resolve_claim_stale_seconds(stale_seconds=stale_seconds)
    try:
        async with aengine.connect() as c, c.begin():
            result = await c.execute(
                build_claim_update(stale_seconds=window, claim_cap=claim_cap),
                _claim_params(run_id, org_id, window, claim_cap),
            )
            return result.fetchone() is not None
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("pipeline_execution.claim_failed run=%s", run_id)
        return False


async def set_rls_org(session: Any, org_id: uuid.UUID) -> None:
    """Set the RLS org context for the session (transaction-scoped on Postgres)."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        await session.execute(
            text("SELECT set_config('app.organisation_id', :val, true)"),
            {"val": str(org_id)},
        )
    else:
        session.info["organisation_id"] = org_id


async def load_and_setup(aeng: AsyncEngine, rid: uuid.UUID, oid: uuid.UUID) -> tuple[Any, Any]:
    """Load the Run and create a PipelineExecutor with checkpointer.

    Returns ``(run, executor)`` or ``(None, None)`` if the run is missing.
    """
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    factory = async_sessionmaker(aeng, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, oid)
        cur = await get_run(session, rid)
        if cur is None:
            _log.warning("Run %s not found during load", rid)
            return None, None

    settings = get_settings()
    conn_string = str(settings.database_url).replace("+asyncpg", "").replace("+psycopg", "")
    executor = PipelineExecutor(aeng, checkpointer_conn_string=conn_string)
    return cur, executor


async def heartbeat_once(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    job: Any = None,
) -> None:
    """Write the DB heartbeat_at and (for SAQ) touch the job hash.

    ``job.update()`` refreshes ``touched`` in the SAQ job hash so the sweeper
    does not re-queue a live run (saq.queue.base.update sets touched=now()).
    """
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
    if job is not None:
        await job.update()


async def heartbeat_loop(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    interval_seconds: int | None = None,
    job: Any = None,
) -> None:
    """Periodic heartbeat every ``RUN_HEARTBEAT_SECONDS`` to keep the run alive."""
    if interval_seconds is None:
        interval_seconds = get_settings().run_heartbeat_seconds
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await heartbeat_once(aeng, run_id, org_id, job=job)
        except asyncio.CancelledError:
            break
        except Exception:
            _log.warning("Heartbeat failed for run %s", run_id)


async def mark_complete(aeng: AsyncEngine, run_id: str, org_id: str) -> None:
    """Mark a still-running run complete using the DB enum value ('complete').

    Idempotent: only transitions a run that is currently ``running`` and never
    overwrites a failure/cancellation/awaiting_human state.
    """
    factory = async_sessionmaker(aeng, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, uuid.UUID(org_id))
        cur = await get_run(session, uuid.UUID(run_id))
        if cur is not None and cur.status == "running":
            cur.status = RUN_COMPLETE_STATUS
            cur.completed_at = datetime.now(UTC)


async def _re_dispatch_capacity_blocked(run_id: str, org_id: str) -> str:
    """Re-dispatch a stranded capacity-blocked run through ``dispatch_run``.

    Re-enters ``claim_run`` → ``execute()`` → ``_check_capacity``, which
    re-checks the org/pipeline cap and either admits the run when a slot frees
    or re-demotes it back to ``pending``. This is the SAME mechanism
    ``dispatcher_reconcile`` uses; the beat sweep is the durable liveness owner
    for capacity-blocked runs because ``dispatcher_reconcile`` deliberately
    excludes them (its exclusion prevents the double-execution double-retry-loop
    race and must stay — see cron_helpers._reconcile_capacity_marker_exclusion).

    Double-execution safety: ``dispatch_run`` enqueues with the deterministic
    ``run:{id}`` SAQ key (deduped if a job already exists) and the worker's
    ``claim_run`` is an atomic ``UPDATE ... WHERE status='pending' OR
    (running AND stale heartbeat)`` — a run already claimed by a live loop
    simply loses the claim.

    Returns the outcome string (``enqueued``/``deferred``/``deduped``/``failed``).
    """
    from modulo.core.dispatch import dispatch_run

    try:
        outcome, _job_id = await dispatch_run(run_id, org_id)
        return outcome
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("pipeline_execution.redispatched_capacity_blocked_failed run=%s", run_id)
        return "failed"


async def stale_run_recovery_sweep(
    async_engine: AsyncEngine,
    *,
    never_dispatched_window: int | None = None,
    worker_lost_window: int | None = None,
) -> dict[str, Any]:
    """Sweep stale pending and running pipeline runs.

    - Pending runs older than the never-dispatched window with no
      ``dispatched_at`` are marked ``failed`` with ``never_dispatched``.
    - Stranded capacity-blocked pending runs (``error_code`` in
      ``org_capacity_limited``/``pipeline_capacity``) whose heartbeat is stale
      are RE-DISPATCHED (durable restart durability — see
      :func:`_re_dispatch_capacity_blocked`), never failed.
    - Capacity-blocked pending runs past ``CAPACITY_TIMEOUT_TTL_MINUTES`` are
      marked ``failed`` with ``capacity_timeout``.
    - Running runs with a heartbeat older than the worker-lost window and
      5+ claims are marked ``failed`` with ``worker_lost``.

    Legacy windows default to today's beat-sweep values — never_dispatched=300s
    (settings ``SAQ_NEVER_DISPATCHED_WINDOW``), worker_lost=600s (settings
    ``SAQ_WORKER_LOST_WINDOW``) — and are deliberately decoupled from
    ``RUN_CLAIM_STALE_SECONDS=450`` (SAQ runs only). The never-dispatched and
    worker-lost branches are scoped to legacy-dispatched rows
    (``dispatcher IS NULL OR dispatcher != 'saq'``) — SAQ runs never carry
    worker_lost/never_dispatched (plan F1). The capacity-timeout backstop is
    SAQ-relevant (capacity-deferred runs) and is NOT dispatcher-scoped.
    """
    settings = get_settings()
    nd_window = never_dispatched_window if never_dispatched_window is not None else settings.saq_never_dispatched_window
    wl_window = worker_lost_window if worker_lost_window is not None else settings.saq_worker_lost_window
    stranded_rows: list[Any] = []
    try:
        # Collect all org ids FIRST in system context (organisations is the
        # root table — the app role owns it, owner bypasses RLS). The sweep's
        # run queries are then scoped PER-ORG via set_config('app.organisation_id')
        # so they are visible under RLS — the pre-existing sweep never called
        # set_rls_org, so under RLS it matched ZERO rows and never recovered
        # anything (Side Effects minor 14, spec §9.4).
        async with async_engine.connect() as conn, conn.begin():
            org_result = await conn.execute(text("SELECT id FROM organisations"))
            org_ids: list[uuid.UUID] = [row[0] for row in org_result.all()]

        if not org_ids:
            return {
                "never_dispatched_swept": 0,
                "worker_lost_swept": 0,
                "capacity_timeout_swept": 0,
                "stranded_capacity_redispatched": 0,
                "redispatch_outcomes": {},
            }

        never_count = 0
        lost_count = 0
        capacity_timeout_count = 0
        stranded_count = 0
        for org_id in org_ids:
            async with async_engine.connect() as conn, conn.begin():
                await conn.execute(
                    text("SELECT set_config('app.organisation_id', :val, true)"),
                    {"val": str(org_id)},
                )
                never_result = await conn.execute(
                    text(
                        "UPDATE runs "
                        "SET status = 'failed', error_code = 'never_dispatched', completed_at = now() "
                        "WHERE status = 'pending' "
                        "AND created_at < now() - (:nd_window * interval '1 second') "
                        "AND dispatched_at IS NULL "
                        "AND cancellation_requested = false "
                        "AND (error_code IS NULL OR error_code NOT IN ('org_capacity_limited', 'pipeline_capacity')) "
                        "AND (dispatcher IS NULL OR dispatcher != 'saq')"
                    ),
                    {"nd_window": nd_window},
                )
                never_count += never_result.rowcount or 0

                stranded_result = await conn.execute(
                    text(
                        "UPDATE runs "
                        "SET heartbeat_at = now() "
                        "WHERE status = 'pending' "
                        "AND error_code IN ('org_capacity_limited', 'pipeline_capacity') "
                        "AND (heartbeat_at IS NULL OR heartbeat_at < now() - (:redispatch_ttl * interval '1 minute')) "
                        "AND created_at >= now() - (:fail_ttl * interval '1 minute') "
                        "AND cancellation_requested = false "
                        "RETURNING id, organisation_id"
                    ),
                    {
                        "redispatch_ttl": _STRANDED_REDISPATCH_TTL_MINUTES,
                        "fail_ttl": CAPACITY_TIMEOUT_TTL_MINUTES,
                    },
                )
                org_stranded_rows = list(stranded_result.all())
                stranded_count += len(org_stranded_rows)
                stranded_rows.extend(org_stranded_rows)

                capacity_timeout_result = await conn.execute(
                    text(
                        "UPDATE runs "
                        "SET status = 'failed', error_code = 'capacity_timeout', completed_at = now() "
                        "WHERE status = 'pending' "
                        "AND error_code IN ('org_capacity_limited', 'pipeline_capacity') "
                        "AND created_at < now() - (:ttl * interval '1 minute') "
                        "AND cancellation_requested = false"
                    ),
                    {"ttl": CAPACITY_TIMEOUT_TTL_MINUTES},
                )
                capacity_timeout_count += capacity_timeout_result.rowcount or 0

                lost_result = await conn.execute(
                    text(
                        "UPDATE runs "
                        "SET status = 'failed', error_code = 'worker_lost', completed_at = now() "
                        "WHERE status = 'running' "
                        "AND heartbeat_at < now() - (:wl_window * interval '1 second') "
                        "AND claim_count >= 5 "
                        "AND (dispatcher IS NULL OR dispatcher != 'saq')"
                    ),
                    {"wl_window": wl_window},
                )
                lost_count += lost_result.rowcount or 0

        # Re-dispatch AFTER each org's sweep transaction commits so dispatch_run's
        # own sessions (and the row lock the UPDATE held) never overlap a live
        # transaction.
        redispatch_outcomes: dict[str, int] = {}
        for row in stranded_rows:
            outcome = await _re_dispatch_capacity_blocked(str(row.id), str(row.organisation_id))
            redispatch_outcomes[outcome] = redispatch_outcomes.get(outcome, 0) + 1

        if never_count or lost_count or capacity_timeout_count or stranded_count:
            _log.info(
                "Stale run recovery: %d never-dispatched, %d capacity-timeout, %d worker-lost runs swept, "
                "%d stranded capacity-blocked runs re-dispatched (%s)",
                never_count,
                capacity_timeout_count,
                lost_count,
                stranded_count,
                redispatch_outcomes,
            )
        return {
            "never_dispatched_swept": never_count,
            "worker_lost_swept": lost_count,
            "capacity_timeout_swept": capacity_timeout_count,
            "stranded_capacity_redispatched": stranded_count,
            "redispatch_outcomes": redispatch_outcomes,
        }
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Stale run recovery sweep failed")
        return {
            "never_dispatched_swept": 0,
            "worker_lost_swept": 0,
            "capacity_timeout_swept": 0,
            "stranded_capacity_redispatched": 0,
            "redispatch_outcomes": {},
            "error": "sweep_failed",
        }


# ---------------------------------------------------------------------------
# HITL resume (plan F6a) — resume_run claim variant + execution
# ---------------------------------------------------------------------------


def build_resume_claim_update(*, stale_seconds: int, claim_cap: int = SAQ_RUN_CLAIM_CAP) -> Any:
    """Build the atomic claim UPDATE for a resumed HITL run.

    Claimable rows (plan F6a):

      * ``status IN ('awaiting_human', 'claimed')`` — the gate decision has
        already been committed by the caller, the run is waiting to resume.
      * ``status = 'running'`` with a stale heartbeat — a mid-resume crash left
        the run running but the worker died.

    The single ``UPDATE ... WHERE ... RETURNING id`` claims atomically
    (no check-then-act window); a concurrent claimer loses because the row
    transitions out of the claimable state in the same statement.
    """
    return text(
        "UPDATE runs SET status='running', heartbeat_at=now(), claim_count=claim_count+1 "
        "WHERE id=:rid AND organisation_id=:oid "
        "AND (status IN ('awaiting_human', 'claimed') "
        "     OR (status = 'running' AND heartbeat_at < now() - (:stale_seconds * interval '1 second'))) "
        "AND claim_count < :claim_cap "
        "RETURNING id"
    )


def _resume_claim_params(run_id: str, org_id: str, stale_seconds: int, claim_cap: int) -> dict[str, object]:
    return {"rid": run_id, "oid": org_id, "stale_seconds": stale_seconds, "claim_cap": claim_cap}


async def claim_resume_run_async(
    aengine: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    claim_cap: int = SAQ_RUN_CLAIM_CAP,
) -> bool:
    """Claim an awaiting_human/claimed (or stale-running) run for resume.

    Idempotent: a second claimer finds the row already ``running`` with a fresh
    heartbeat and loses the atomic UPDATE. The gate decision itself is committed
    by the caller (HITL endpoints / recover-node) before dispatch.
    """
    stale_seconds = int(get_settings().run_claim_stale_seconds)
    try:
        async with aengine.connect() as c, c.begin():
            result = await c.execute(
                build_resume_claim_update(stale_seconds=stale_seconds, claim_cap=claim_cap),
                _resume_claim_params(run_id, org_id, stale_seconds, claim_cap),
            )
            return result.fetchone() is not None
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("pipeline_execution.resume_claim_failed run=%s", run_id)
        return False


async def resume_run(
    *,
    async_engine: AsyncEngine,
    run_id: str,
    org_id: str,
    resume_data: dict[str, Any] | None = None,
    job: Any = None,
    claim_cap: int = SAQ_RUN_CLAIM_CAP,
) -> dict[str, Any]:
    """Resume an interrupted HITL run (SAQ ``resume_run`` job).

    Claims the run via :func:`claim_resume_run_async`, loads the executor, and
    streams the graph from the checkpoint with *resume_data* as the gate
    decision (plan F6a). Mirrors :func:`execute_run`'s cancellation-safe
    heartbeat/complete structure: the heartbeat loop is cancelled in ``finally``
    and completion is written by :func:`mark_complete` (genuine completion only).
    """
    rid = uuid.UUID(run_id)
    oid = uuid.UUID(org_id)

    claimed = await claim_resume_run_async(async_engine, str(rid), str(oid), claim_cap=claim_cap)
    if not claimed:
        _log.warning("resume_run: run %s not claimed (wrong state or claim cap)", rid)
        return {"status": "not_claimed"}

    run, executor = await load_and_setup(async_engine, rid, oid)
    if run is None:
        return {"status": "missing"}

    heartbeat_task: asyncio.Task[Any] | None = None
    try:
        heartbeat_task = asyncio.create_task(  # nosemgrep: create-task-without-guard
            heartbeat_loop(async_engine, str(rid), str(oid), job=job),
            name=f"resume-heartbeat-{rid}",
        )
        await executor.resume(run_id=rid, org_id=oid, resume_data=resume_data or {})
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("resume_run failed for run %s", rid)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    await mark_complete(async_engine, str(rid), str(oid))
    return {"status": "complete"}
