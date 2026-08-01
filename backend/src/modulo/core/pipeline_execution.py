"""Shared pipeline execution core for Celery and SAQ.

This module is the single home for the claim / execute / heartbeat / complete /
stale-sweep logic that was historically embedded in
:mod:`modulo.core.pipeline_executor_task` (the Celery task module deleted by
PR C of the Celery->SAQ migration). SAQ workers (PR B) and the Celery task both
delegate here.

NOT here: ``dispatch_run`` (PR B), cron firing (PR B), fire/report jobs.

Engine injection: every entry point takes its engine(s) explicitly so the
Celery prefork path can keep using its sync claim pool + async execution pool
while the SAQ path passes its own async engine. No module-level engine globals.

Staleness constants (plan F4 / F1 ordering):

    RUN_CLAIM_STALE_SECONDS        = 450  SAQ runs only
    LEGACY_RUN_CLAIM_STALE_SECONDS = 180  Celery/legacy path (today's 3-minute window)
    SAQ_JOB_HEARTBEAT              = 300  SAQ job heartbeat knob
    RUN_HEARTBEAT_SECONDS          = 30   DB heartbeat cadence

Both claim staleness values are configurable via settings (see the F4 Settings
section in :mod:`modulo.settings`). The legacy sweep windows default to
never_dispatched=300 / worker_lost=600 (today's beat-sweep values, 5 and 10
minutes) and stay decoupled from ``RUN_CLAIM_STALE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Session

from modulo.db.crud.run import get_run

_log = logging.getLogger(__name__)

# Claim staleness gates (configurable via settings; module defaults are the
# documented SAQ / legacy values).
RUN_CLAIM_STALE_SECONDS = 450
LEGACY_RUN_CLAIM_STALE_SECONDS = 180

# DB heartbeat cadence (F4). Must stay well below the 300s SAQ sweep threshold.
RUN_HEARTBEAT_SECONDS = 30
SAQ_JOB_HEARTBEAT = 300

# Terminal "success" status written by _mark_complete — MUST match the runs
# status CHECK constraint ('complete', NOT 'completed'). See
# db/models/run.py:ck_runs_status.
RUN_COMPLETE_STATUS = "complete"

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
    the SAQ scheduler modules never import the Celery task module that PR C
    deletes.
    """

    pass


def _make_sync_url(database_url: str) -> str:
    """Convert async DB URL to sync by replacing async driver with sync equivalent."""
    return (
        database_url.replace("+asyncpg", "+psycopg").replace("+aiomysql", "+mysqldb").replace("+aiosqlite", "+pysqlite")
    )


_sync_beat_engine: Any = None
_sync_beat_lock = threading.Lock()


def get_beat_sync_session() -> Session:
    """Return a sync SQLAlchemy session for the Celery beat scheduler.

    Uses a dedicated engine separate from the worker's sync pool to avoid
    contention between beat polling and task execution. Relocated from
    ``modulo.core.pipeline_executor_task`` (PR B-2, plan F1).
    """
    global _sync_beat_engine
    if _sync_beat_engine is None:
        with _sync_beat_lock:
            if _sync_beat_engine is None:
                s = get_settings()
                sync_url = _make_sync_url(str(s.database_url))
                _sync_beat_engine = create_engine(
                    sync_url,
                    pool_size=s.modulo_celery_db_pool_sync_size,
                    max_overflow=s.modulo_celery_db_pool_sync_overflow,
                    pool_pre_ping=True,
                    pool_recycle=300,
                    pool_use_lifo=False,
                    pool_timeout=s.modulo_celery_db_pool_sync_timeout,
                )
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=_sync_beat_engine)()


def dispose_beat_sync_engine() -> None:
    global _sync_beat_engine
    if _sync_beat_engine is not None:
        _sync_beat_engine.dispose()
        _sync_beat_engine = None


def _resolve_claim_stale_seconds(*, legacy: bool, stale_seconds: int | None) -> int:
    """Resolve the claim staleness window.

    The SAQ path uses ``RUN_CLAIM_STALE_SECONDS`` (450); the legacy Celery path
    keeps today's 180s window (``interval '3 minutes'``). Both are configurable
    via settings.
    An explicit ``stale_seconds`` overrides settings (used by tests and by the
    SAQ reconcile path later).
    """
    if stale_seconds is not None:
        return stale_seconds
    settings = get_settings()
    if legacy:
        return int(settings.legacy_run_claim_stale_seconds)
    return int(settings.run_claim_stale_seconds)


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


def claim_run(
    engine: Engine,
    run_id: str,
    org_id: str,
    stale_seconds: int | None = None,
    *,
    legacy: bool = False,
    claim_cap: int = _DEFAULT_CLAIM_CAP,
) -> bool:
    """Claim a pending or stale-running run via an atomic SQL update (sync).

    Returns True if the row was claimed, False if already handled or the claim
    failed. Used by the Celery prefork path (legacy claim window).
    """
    window = _resolve_claim_stale_seconds(legacy=legacy, stale_seconds=stale_seconds)
    try:
        with engine.connect() as c, c.begin():
            result = c.execute(
                build_claim_update(stale_seconds=window, claim_cap=claim_cap),
                _claim_params(run_id, org_id, window, claim_cap),
            )
            return result.fetchone() is not None
    except Exception:
        _log.exception("pipeline_execution.claim_failed run=%s", run_id)
        return False


async def claim_run_async(
    aengine: AsyncEngine,
    run_id: str,
    org_id: str,
    stale_seconds: int | None = None,
    *,
    legacy: bool = False,
    claim_cap: int = _DEFAULT_CLAIM_CAP,
) -> bool:
    """Claim a pending or stale-running run via an atomic SQL update (async).

    Same semantics as :func:`claim_run`; used by the SAQ execute path.
    """
    window = _resolve_claim_stale_seconds(legacy=legacy, stale_seconds=stale_seconds)
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


async def execute_run(
    *,
    sync_engine: Engine,
    async_engine: AsyncEngine,
    run_id: str,
    org_id: str,
    legacy_claim: bool = True,
) -> None:
    """Execute a single pipeline run from claim through completion.

    Shared by the Celery task and (from PR B) the SAQ ``execute_run`` job.
    ``legacy_claim`` selects the legacy 180s claim window (Celery) vs the SAQ
    450s window. ``_task_instance`` is intentionally absent (plan F4: engine
    injection, no task coupling).
    """
    rid = uuid.UUID(run_id)
    oid = uuid.UUID(org_id)

    claimed = claim_run(sync_engine, str(rid), str(oid), legacy=legacy_claim)
    if not claimed:
        _log.warning("Run %s not claimed — already handled or in wrong state", run_id)
        return

    cur, executor = await load_and_setup(async_engine, rid, oid)
    if cur is None:
        return

    heartbeat_task: asyncio.Task[Any] | None = None
    try:
        # execute_run is an async coroutine (always a running loop) — the
        # create-task-without-guard rule targets sync signal/listener code.
        heartbeat_task = asyncio.create_task(  # nosemgrep: create-task-without-guard
            heartbeat_loop(async_engine, str(rid), str(oid)),
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

    await mark_complete(async_engine, str(rid), str(oid))


async def stale_run_recovery_sweep(
    async_engine: AsyncEngine,
    *,
    never_dispatched_window: int | None = None,
    worker_lost_window: int | None = None,
) -> dict[str, Any]:
    """Sweep stale pending and running pipeline runs.

    - Pending runs older than the never-dispatched window with no
      ``dispatched_at`` are marked ``failed`` with ``never_dispatched``.
    - Running runs with a heartbeat older than the worker-lost window and
      5+ claims are marked ``failed`` with ``worker_lost``.

    Legacy windows default to today's beat-sweep values — never_dispatched=300s
    (settings ``SAQ_NEVER_DISPATCHED_WINDOW``), worker_lost=600s (settings
    ``SAQ_WORKER_LOST_WINDOW``) — and are deliberately decoupled from
    ``RUN_CLAIM_STALE_SECONDS=450`` (SAQ runs only). PR B scopes this sweep to
    legacy-dispatched runs once the ``dispatcher`` column exists.
    """
    settings = get_settings()
    nd_window = never_dispatched_window if never_dispatched_window is not None else settings.saq_never_dispatched_window
    wl_window = worker_lost_window if worker_lost_window is not None else settings.saq_worker_lost_window
    try:
        async with async_engine.connect() as conn, conn.begin():
            never_result = await conn.execute(
                text(
                    "UPDATE runs "
                    "SET status = 'failed', error_code = 'never_dispatched', completed_at = now() "
                    "WHERE status = 'pending' "
                    "AND created_at < now() - (:nd_window * interval '1 second') "
                    "AND dispatched_at IS NULL"
                ),
                {"nd_window": nd_window},
            )
            never_count = never_result.rowcount

            lost_result = await conn.execute(
                text(
                    "UPDATE runs "
                    "SET status = 'failed', error_code = 'worker_lost', completed_at = now() "
                    "WHERE status = 'running' "
                    "AND heartbeat_at < now() - (:wl_window * interval '1 second') "
                    "AND claim_count >= 5"
                ),
                {"wl_window": wl_window},
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
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Stale run recovery sweep failed")
        return {
            "never_dispatched_swept": 0,
            "worker_lost_swept": 0,
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
