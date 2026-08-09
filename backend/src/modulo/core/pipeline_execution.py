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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.errors import NodeCancelledError
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
# ``pending`` with a capacity marker stays pending — there is NO in-process
# retry loop since the Tier 3 removal of ``_retry_pending`` (plan F3b) — and is
# recovered by the durable sweep paths: ``dispatcher_reconcile`` re-dispatches
# it when capacity frees; ``stale_run_recovery_sweep`` re-dispatches stranded
# capacity-blocked runs whose heartbeat is stale (the in-process loop is
# provably gone); and the ``CAPACITY_TIMEOUT_TTL_MINUTES`` backstop
# TERMINAL-FAILS a legitimate never-executed run past the 120-min window.
# This window re-dispatches a stranded run long before that backstop — but
# ONLY when its heartbeat is stale (the in-process loop is provably gone), and
# never once it is already past the capacity_timeout TTL (those must fail, not
# be resurrected forever).
_STRANDED_REDISPATCH_TTL_MINUTES = 12

# Claim caps are a SINGLE source of truth: ``SAQ_RUN_CLAIM_CAP`` in settings
# (default 20). Execute (plan F4) and resume (plan F6a) claims both resolve the
# cap from settings via :func:`_resolve_claim_cap` — the old execute-only
# ``_DEFAULT_CLAIM_CAP=5`` firefight value was retired (retro item 9). SAQ
# retries reuse the same saq_job_id, so the cap bounds re-claims on an
# at-most-once boundary.

# Zombie-run error codes (2026-08-05). A claimed run that never dispatches a
# node must be TERMINAL-FAILED (never left 'running' with a live heartbeat):
#   - ``executor_setup_failed``: load_and_setup / executor setup raised (e.g.
#     a DB OperationalError during checkpointer or graph setup) before any node
#     could run.
#   - ``executor_stalled``: the execute_run zombie watchdog found the executor
#     still running with zero node progress after SAQ_SETUP_GRACE_SECONDS and
#     cancelled it.
EXECUTOR_SETUP_FAILED_ERROR_CODE = "executor_setup_failed"
EXECUTOR_STALLED_ERROR_CODE = "executor_stalled"


# E2B idempotency fence (plan F3a): the run-level dispatch lock is kept until
# the run is terminal, bounded by an ~8h upper TTL (>= execute_run timeout 7200s
# * retries 5 + margin). A successor claim can only re-dispatch after a fenced
# release (dispatch failure / teardown) or terminal DEL.
E2B_IDEMPOTENCY_TTL_SECONDS = 8 * 3600


class ClaimSupersededError(Exception):
    """Raised when this executor's claim token no longer matches the run's current claim.

    Signals a superseded executor (a successor re-claimed the run after an
    event-loop stall) so it aborts before overwriting the successor's state.
    """


class E2BIdempotencyError(Exception):
    """Base error for the E2B dispatch idempotency fence."""


class E2BIdempotencyDeniedError(E2BIdempotencyError):
    """The E2B dispatch fence was already won — do not create a second sandbox."""


def get_settings() -> Any:
    from modulo.settings import get_settings as _get_settings

    return _get_settings()


class SchedulerDBError(Exception):
    """Raised when a scheduler DB query fails transiently.

    Relocated from ``modulo.core.pipeline_executor_task`` (PR B-2, plan F1) so
    the SAQ scheduler modules never import the deleted Celery task module.
    """


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


def _resolve_claim_cap(claim_cap: int | None) -> int:
    """Resolve the per-claim cap from settings (single source of truth, retro 9).

    Reads ``get_settings().saq_run_claim_cap`` (default 20, alias
    ``SAQ_RUN_CLAIM_CAP``). An explicit ``claim_cap`` overrides settings (used
    by tests). Execute and resume claims share this one knob — the old
    execute-only ``_DEFAULT_CLAIM_CAP=5`` firefight value is retired.
    """
    if claim_cap is not None:
        return claim_cap
    return int(get_settings().saq_run_claim_cap)


_CLAIM_UPDATE_SQL = text(
    "UPDATE runs SET status='running', heartbeat_at=now(), claim_count=claim_count+1 "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND (status = 'pending' "
    "     OR (status = 'running' AND heartbeat_at < now() - (:stale_seconds * interval '1 second'))) "
    "AND claim_count < :claim_cap "
    "RETURNING id"
)

_CLAIM_UPDATE_SQL_WITH_TOKEN = text(
    "UPDATE runs SET status='running', heartbeat_at=now(), claim_count=claim_count+1, claim_token=:tok "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND (status = 'pending' "
    "     OR (status = 'running' AND heartbeat_at < now() - (:stale_seconds * interval '1 second'))) "
    "AND claim_count < :claim_cap "
    "RETURNING id"
)


def build_claim_update(
    *,
    stale_seconds: int,
    claim_cap: int | None = None,
    claim_token: str | None = None,
) -> Any:
    """Build the atomic claim UPDATE for a pipeline run.

    The statement is a single ``UPDATE ... WHERE ... RETURNING id``: exactly one
    concurrent claimer wins because the row transitions out of the claimable
    state in the same statement that claims it (no check-then-act window).

    Claimable rows:
      * ``status = 'pending'`` always.
      * ``status = 'running'`` when the heartbeat is older than *stale_seconds*.

    ``claim_cap`` bounds the number of claims (claim_count) per run; callers
    resolve it from settings (``SAQ_RUN_CLAIM_CAP``, default 20) via
    :func:`_resolve_claim_cap` — the value is bound at execute time, not baked
    into this template.

    When *claim_token* is given the claim also rotates ``runs.claim_token`` to a
    FRESH per-claim value (plan F3a) — each re-claim gets a distinct token so a
    superseded original's heartbeat/E2B fence can detect it was replaced.

    Callers pass the full parameter dict (rid / oid / stale_seconds / claim_cap)
    at execute time.
    """
    if claim_token is not None:
        return _CLAIM_UPDATE_SQL_WITH_TOKEN
    return _CLAIM_UPDATE_SQL


def _claim_params(
    run_id: str,
    org_id: str,
    stale_seconds: int,
    claim_cap: int,
    claim_token: str | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {"rid": run_id, "oid": org_id, "stale_seconds": stale_seconds, "claim_cap": claim_cap}
    if claim_token is not None:
        params["tok"] = claim_token
    return params


async def _maybe_alert_retry_storm(aengine: AsyncEngine, run_id: str, org_id: str) -> None:
    """Best-effort SAQ retry-storm alert (plan F1 probe 6 / F3a).

    Fires an error_event (source='saq') when a re-claim pushes the run's
    ``claim_count`` past the threshold in
    :func:`modulo.core.error_tracking.emit_saq_retry_storm_alert`. Runs only
    after a successful claim and never breaks the claim path (best-effort).
    """
    try:
        async with aengine.connect() as c:
            await c.execute(
                text("SELECT set_config('app.organisation_id', :val, true)"),
                {"val": org_id},
            )
            result = await c.execute(text("SELECT claim_count FROM runs WHERE id=:rid"), {"rid": run_id})
            row = result.first()
        if row is None:
            return
        from modulo.core.error_tracking import emit_saq_retry_storm_alert

        await emit_saq_retry_storm_alert(aengine, org_id, run_id, int(row[0]))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("pipeline_execution.retry_storm_alert_failed run=%s", run_id)


async def claim_run_async(
    aengine: AsyncEngine,
    run_id: str,
    org_id: str,
    stale_seconds: int | None = None,
    *,
    claim_cap: int | None = None,
) -> str | None:
    """Claim a pending or stale-running run via an atomic SQL update (async).

    Used by the SAQ execute path. Rotates ``runs.claim_token`` to a fresh
    per-claim value (plan F3a) so a superseded original executor can detect it
    was replaced.

    ``claim_cap`` bounds the number of claims (claim_count) per run; when
    omitted it resolves from settings (``SAQ_RUN_CLAIM_CAP``, default 20) via
    :func:`_resolve_claim_cap`.

    Returns the fresh claim token when the row was claimed, or ``None`` when
    the run is not claimable (or the claim failed). The token is threaded into
    ``heartbeat_loop``/``mark_complete`` so a superseded original can neither
    complete the run out from under a successor nor DEL its E2B dispatch key.
    """
    window = _resolve_claim_stale_seconds(stale_seconds=stale_seconds)
    cap = _resolve_claim_cap(claim_cap)
    claim_token = uuid.uuid4().hex
    try:
        async with aengine.connect() as c, c.begin():
            result = await c.execute(
                build_claim_update(stale_seconds=window, claim_cap=cap, claim_token=claim_token),
                _claim_params(run_id, org_id, window, cap, claim_token),
            )
            claimed = result.fetchone() is not None
        if claimed:
            await _maybe_alert_retry_storm(aengine, run_id, org_id)
        return claim_token if claimed else None
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("pipeline_execution.claim_failed run=%s", run_id)
        return None


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


async def _read_current_claim_token(aeng: AsyncEngine, run_id: str, org_id: str) -> str | None:
    """Read the run's current ``claim_token`` from the DB (RLS-scoped)."""
    async with aeng.connect() as c:
        await c.execute(
            text("SELECT set_config('app.organisation_id', :val, true)"),
            {"val": org_id},
        )
        result = await c.execute(text("SELECT claim_token FROM runs WHERE id=:rid"), {"rid": run_id})
        row = result.first()
        return str(row[0]) if row and row[0] else None


async def heartbeat_once(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    job: Any = None,
    claim_token: str | None = None,
) -> None:
    """Write the DB heartbeat_at and (for SAQ) touch the job hash.

    ``job.update()`` refreshes ``touched`` in the SAQ job hash so the sweeper
    does not re-queue a live run (saq.queue.base.update sets touched=now()).

    When *claim_token* is provided the write is fenced (plan F3a): the run's
    current ``claim_token`` must still match this executor's token, otherwise a
    superseded original could overwrite the successor's fresh heartbeat. A
    mismatch raises :class:`ClaimSupersededError` BEFORE any DB write.
    """
    if claim_token is not None:
        current = await _read_current_claim_token(aeng, run_id, org_id)
        if current is not None and current != claim_token:
            raise ClaimSupersededError(
                f"claim token superseded for run {run_id} (had {claim_token}, current {current})"
            )
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
    claim_token: str | None = None,
) -> None:
    """Periodic heartbeat every ``RUN_HEARTBEAT_SECONDS`` to keep the run alive.

    The executor's claim token is captured at loop start (the claim just wrote
    it) and used to fence every heartbeat: once a successor re-claims the run
    and rotates the token, the superseded original aborts its heartbeat instead
    of overwriting the successor's fresh heartbeat.
    """
    if interval_seconds is None:
        interval_seconds = get_settings().run_heartbeat_seconds
    if claim_token is None:
        claim_token = await _read_current_claim_token(aeng, run_id, org_id)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await heartbeat_once(aeng, run_id, org_id, job=job, claim_token=claim_token)
        except ClaimSupersededError:
            _log.warning("Heartbeat superseded for run %s — aborting heartbeat", run_id)
            break
        except asyncio.CancelledError:
            break
        except Exception:
            _log.warning("Heartbeat failed for run %s", run_id)


async def mark_complete(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    claim_token: str | None = None,
) -> None:
    """Mark a still-running run complete using the DB enum value ('complete').

    Idempotent: only transitions a run that is currently ``running`` and never
    overwrites a failure/cancellation/awaiting_human state.

    When *claim_token* is provided, completion is fenced (plan F3a): a superseded
    original (claim token rotated by a successor) cannot complete the run out
    from under the successor. On completion the E2B idempotency key is released
    (plan F3a — terminal DEL).
    """
    factory = async_sessionmaker(aeng, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, uuid.UUID(org_id))
        cur = await get_run(session, uuid.UUID(run_id))
        if cur is not None and cur.status == "running":
            if claim_token is not None and getattr(cur, "claim_token", None) != claim_token:
                _log.warning("mark_complete skipped for run %s (claim superseded)", run_id)
                return
            cur.status = RUN_COMPLETE_STATUS
            cur.completed_at = datetime.now(UTC)
    try:
        if e2b_idempotency_enabled():
            await e2b_dispatch_release_terminal(run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("mark_complete: E2B idempotency key release failed for run %s", run_id)


async def fail_run_terminal(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    error_code: str,
    error_detail: str,
) -> bool:
    """Terminal-fail a claimed-but-stuck run (zombie protection).

    Only transitions a run that is currently ``running`` (a run already
    terminal, or capacity-deferred back to ``pending``, is left untouched —
    the capacity machinery owns pending runs). Idempotent and safe against a
    concurrent claimer: the ``running`` guard plus the row write inside the
    transaction means a second claimer that already reset the run to
    ``running`` simply overwrites with the same failure.
    """
    factory = async_sessionmaker(aeng, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, uuid.UUID(org_id))
        cur = await get_run(session, uuid.UUID(run_id))
        if cur is None or cur.status != "running":
            return False
        cur.status = "failed"
        cur.error_code = error_code
        cur.error_detail = error_detail[:5000]
        cur.completed_at = datetime.now(UTC)
    _log.warning(
        "run.terminal_failed run=%s error_code=%s",
        run_id,
        error_code,
    )
    return True


async def zombie_watchdog(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    first_progress: asyncio.Event,
    *,
    exec_task: asyncio.Task[Any],
    stall_requested: asyncio.Event | None = None,
    grace_seconds: int | None = None,
) -> None:
    """Fail a claimed-but-nodeless run when no node dispatches in time.

    The heartbeat loop starts before ``executor.execute`` so a run hung in the
    pre-node setup window (checkpointer setup, graph compile, connector hub
    init, a DB ``OperationalError``) would otherwise stay ``running`` forever
    with a fresh heartbeat. This watchdog bounds that window: it waits up to
    *grace_seconds* (default ``SAQ_SETUP_GRACE_SECONDS``) for the executor to
    signal first progress (first node dispatched via ``on_first_progress``).

    If the executor task finishes first (completion, exception, or
    capacity-deferral back to ``pending``) the watchdog stands down — a
    capacity-deferred run is NOT failed. If the window elapses with the
    executor still running and zero node progress, the watchdog cancels the
    executor task, signals *stall_requested* (so the wrapper can tell a
    watchdog-initiated cancellation from a worker shutdown), and terminal-fails
    the run (``executor_stalled``). Cancelling the executor FIRST ensures a
    late-returning ``execute`` cannot overwrite the failure through
    ``finalize_cost``.
    """
    if grace_seconds is None:
        grace_seconds = int(get_settings().saq_setup_grace_seconds)
    try:
        await asyncio.wait_for(first_progress.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        raise

    if exec_task.done():
        return

    _log.warning(
        "zombie_watchdog.stalled run=%s no node dispatched within %ds — cancelling executor and failing run",
        run_id,
        grace_seconds,
    )
    exec_task.cancel()
    if stall_requested is not None:
        stall_requested.set()
    await fail_run_terminal(
        aeng,
        run_id,
        org_id,
        error_code=EXECUTOR_STALLED_ERROR_CODE,
        error_detail=(
            f"Executor dispatched no node within {grace_seconds}s setup grace (claimed-but-nodeless zombie watchdog)"
        ),
    )


async def run_executor_with_watchdog(
    aeng: AsyncEngine,
    *,
    run_id: str,
    org_id: str,
    executor: Any,
    job: Any,
    execute_fn: Callable[[], Awaitable[Any]],
    claim_token: str | None = None,
) -> dict[str, Any]:
    """Run ``execute_fn`` under the DB heartbeat loop + zombie watchdog.

    Shared by ``saq_worker.execute_run`` and ``resume_run``. Expected flow:

    * The caller has already claimed the run (``status='running'``) and loaded
      the executor via :func:`load_and_setup`.
    * The executor must expose ``on_first_progress`` (a no-arg callable); this
      helper wires it to an :class:`asyncio.Event` that the zombie watchdog
      waits on. The executor calls it when the first node dispatches.
    * The heartbeat loop starts concurrently (as today) and keeps the run alive
      during legitimate node execution.
    * If no progress is signalled within ``SAQ_SETUP_GRACE_SECONDS`` the
      watchdog cancels the executor and fails the run (``executor_stalled``) —
      this is the fix for the 30h+ zombies seen on app.modulo.run.
    * An ``asyncio.CancelledError`` raised by the executor task is swallowed
      ONLY when the watchdog caused it (the run is already terminal); a worker
      shutdown cancellation re-raises. Because the watchdog is still inside its
      ``fail_run_terminal`` DB write when this handler runs, it is awaited to
      completion here before the ``finally`` block cancels it — cancelling the
      watchdog mid-write would abort the terminal-fail transaction and leave
      the run ``running`` forever.

    Returns ``{"status": "complete"}`` — the caller still runs
    ``mark_complete`` (a no-op once the run is terminal).
    """
    rid = uuid.UUID(run_id)

    first_progress = asyncio.Event()
    stall_requested = asyncio.Event()
    if executor is not None:
        executor.on_first_progress = first_progress.set

    async def _execute() -> Any:
        return await execute_fn()

    exec_task = asyncio.create_task(_execute(), name=f"saq-exec-{rid}")
    watchdog_task = asyncio.create_task(
        zombie_watchdog(
            aeng,
            run_id,
            org_id,
            first_progress,
            exec_task=exec_task,
            stall_requested=stall_requested,
        ),
        name=f"saq-zombie-watchdog-{rid}",
    )
    heartbeat_task = asyncio.create_task(  # nosemgrep: create-task-without-guard
        heartbeat_loop(aeng, run_id, org_id, job=job, claim_token=claim_token),
        name=f"saq-heartbeat-{rid}",
    )
    try:
        await exec_task
    except asyncio.CancelledError:
        # Distinguish watchdog-initiated cancellation from worker shutdown:
        # the watchdog cancels ONLY the executor task and signals
        # ``stall_requested`` as it starts failing the run. Await it to
        # completion so its ``fail_run_terminal`` transaction commits before
        # the ``finally`` below cancels the watchdog — otherwise the terminal
        # write is aborted, the run stays ``running``, and a stray
        # ``CancelledError`` leaks into the SAQ worker. A genuine
        # ``fail_run_terminal`` error propagates so the reconcile net remains
        # the backstop instead of ``mark_complete`` wrongly succeeding the run.
        if stall_requested.is_set():
            if watchdog_task is not None and not watchdog_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog_task
            _log.warning("run_executor_with_watchdog: execution cancelled by zombie watchdog for run %s", rid)
        else:
            raise
    except NodeCancelledError:
        # Transient node cancellation — execute() already reset the run to
        # pending and released the E2B fence; re-raise so SAQ retries the job.
        raise
    except Exception:
        _log.exception("run_executor_with_watchdog: execute failed for run %s", rid)
    finally:
        if watchdog_task is not None:
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
        if exec_task is not None and not exec_task.done():
            exec_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await exec_task
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    return {"status": "complete"}


async def _re_dispatch_capacity_blocked(run_id: str, org_id: str) -> str:
    """Re-dispatch a stranded capacity-blocked run through ``dispatch_run``.

    Re-enters ``claim_run`` → ``execute()`` → ``_check_capacity``, which
    re-checks the org/pipeline cap and either admits the run when a slot frees
    or re-demotes it back to ``pending``. This is the SAME mechanism
    ``dispatcher_reconcile`` uses; the beat sweep remains the durable liveness
    backstop for capacity-blocked runs. ``dispatcher_reconcile`` admits a
    pending capacity-marked run only once its heartbeat is stale (or NULL) —
    the heartbeat gate throttles the executor sandbox-cap claim/demote churn
    loop to one attempt per ``CAPACITY_REDISPATCH_SECONDS`` (FAR-108), so a
    fresh-heartbeat row still has exactly one re-dispatch owner. See
    cron_helpers._reconcile_capacity_marker_exclusion.

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


_RESUME_CLAIM_UPDATE_SQL = text(
    "UPDATE runs SET status='running', heartbeat_at=now(), claim_count=claim_count+1 "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND (status IN ('awaiting_human', 'claimed') "
    "     OR (status = 'running' AND heartbeat_at < now() - (:stale_seconds * interval '1 second'))) "
    "AND claim_count < :claim_cap "
    "RETURNING id"
)

_RESUME_CLAIM_UPDATE_SQL_WITH_TOKEN = text(
    "UPDATE runs SET status='running', heartbeat_at=now(), claim_count=claim_count+1, claim_token=:tok "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND (status IN ('awaiting_human', 'claimed') "
    "     OR (status = 'running' AND heartbeat_at < now() - (:stale_seconds * interval '1 second'))) "
    "AND claim_count < :claim_cap "
    "RETURNING id"
)


def build_resume_claim_update(
    *,
    stale_seconds: int,
    claim_cap: int | None = None,
    claim_token: str | None = None,
) -> Any:
    """Build the atomic claim UPDATE for a resumed HITL run.

    Claimable rows (plan F6a):

      * ``status IN ('awaiting_human', 'claimed')`` — the gate decision has
        already been committed by the caller, the run is waiting to resume.
      * ``status = 'running'`` with a stale heartbeat — a mid-resume crash left
        the run running but the worker died.

    The single ``UPDATE ... WHERE ... RETURNING id`` claims atomically
    (no check-then-act window); a concurrent claimer loses because the row
    transitions out of the claimable state in the same statement.

    ``claim_cap`` bounds the number of claims (claim_count) per run; callers
    resolve it from settings (``SAQ_RUN_CLAIM_CAP``, default 20) via
    :func:`_resolve_claim_cap` — the value is bound at execute time, not baked
    into this template.

    When *claim_token* is given the claim rotates ``runs.claim_token`` to a
    fresh per-claim value (plan F3a).
    """
    if claim_token is not None:
        return _RESUME_CLAIM_UPDATE_SQL_WITH_TOKEN
    return _RESUME_CLAIM_UPDATE_SQL


def _resume_claim_params(
    run_id: str,
    org_id: str,
    stale_seconds: int,
    claim_cap: int,
    claim_token: str | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {"rid": run_id, "oid": org_id, "stale_seconds": stale_seconds, "claim_cap": claim_cap}
    if claim_token is not None:
        params["tok"] = claim_token
    return params


async def claim_resume_run_async(
    aengine: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    claim_cap: int | None = None,
) -> str | None:
    """Claim an awaiting_human/claimed (or stale-running) run for resume.

    Idempotent: a second claimer finds the row already ``running`` with a fresh
    heartbeat and loses the atomic UPDATE. The gate decision itself is committed
    by the caller (HITL endpoints / recover-node) before dispatch. Rotates
    ``runs.claim_token`` to a fresh per-claim value (plan F3a).

    ``claim_cap`` bounds the number of claims (claim_count) per run; when
    omitted it resolves from settings (``SAQ_RUN_CLAIM_CAP``, default 20) via
    :func:`_resolve_claim_cap`.

    Returns the fresh claim token when the row was claimed, or ``None`` when it
    is not claimable (or the claim failed) — threaded into ``heartbeat_loop``/
    ``mark_complete`` so a superseded original cannot complete the run or DEL
    the successor's E2B dispatch key.
    """
    stale_seconds = int(get_settings().run_claim_stale_seconds)
    cap = _resolve_claim_cap(claim_cap)
    claim_token = uuid.uuid4().hex
    try:
        async with aengine.connect() as c, c.begin():
            result = await c.execute(
                build_resume_claim_update(stale_seconds=stale_seconds, claim_cap=cap, claim_token=claim_token),
                _resume_claim_params(run_id, org_id, stale_seconds, cap, claim_token),
            )
            claimed = result.fetchone() is not None
        return claim_token if claimed else None
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("pipeline_execution.resume_claim_failed run=%s", run_id)
        return None


async def resume_run(
    *,
    async_engine: AsyncEngine,
    run_id: str,
    org_id: str,
    resume_data: dict[str, Any] | None = None,
    job: Any = None,
    claim_cap: int | None = None,
) -> dict[str, Any]:
    """Resume an interrupted HITL run (SAQ ``resume_run`` job).

    Claims the run via :func:`claim_resume_run_async`, loads the executor, and
    streams the graph from the checkpoint with *resume_data* as the gate
    decision (plan F6a). Mirrors :func:`execute_run`'s cancellation-safe
    heartbeat/complete structure and shares the zombie watchdog: a resume that
    hangs in the pre-stream setup window (checkpointer reload, graph compile)
    is terminal-failed by :func:`zombie_watchdog` instead of running forever.
    The heartbeat loop is cancelled in ``finally`` and completion is written by
    :func:`mark_complete` (genuine completion only).

    ``claim_cap`` bounds the number of claims (claim_count) per run; when
    omitted it resolves from settings (``SAQ_RUN_CLAIM_CAP``, default 20) via
    :func:`_resolve_claim_cap`.
    """
    rid = uuid.UUID(run_id)
    oid = uuid.UUID(org_id)

    cap = _resolve_claim_cap(claim_cap)
    claim_token = await claim_resume_run_async(async_engine, str(rid), str(oid), claim_cap=cap)
    if not claim_token:
        _log.warning("resume_run: run %s not claimed (wrong state or claim cap)", rid)
        return {"status": "not_claimed"}

    try:
        run, executor = await load_and_setup(async_engine, rid, oid)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("resume_run: load_and_setup failed for run %s", rid)
        await fail_run_terminal(
            async_engine,
            str(rid),
            str(oid),
            error_code=EXECUTOR_SETUP_FAILED_ERROR_CODE,
            error_detail="load_and_setup failed during resume (pre-stream setup)",
        )
        return {"status": "setup_failed"}
    if run is None:
        return {"status": "missing"}

    await run_executor_with_watchdog(
        async_engine,
        run_id=str(rid),
        org_id=str(oid),
        executor=executor,
        job=job,
        claim_token=claim_token,
        execute_fn=lambda: executor.resume(run_id=rid, org_id=oid, resume_data=resume_data or {}),
    )

    await mark_complete(async_engine, str(rid), str(oid), claim_token=claim_token)
    return {"status": "complete"}


# ---------------------------------------------------------------------------
# E2B dispatch idempotency fence (plan F3a)
#
# The at-most-once mitigation for event-loop stalls (>= RUN_CLAIM_STALE_SECONDS):
# a superseded executor must never create a second sandbox for the same run.
#
# Mechanism — a RUN-LEVEL Redis key ``run:{run_id}:e2b`` storing the claim token:
#
#   * ``e2b_dispatch_acquire`` SETNX-before-dispatch (atomic): exactly one
#     executor wins. If the key already exists, the dispatch is ABORTED whether
#     the value is our token (a live dispatch within the same claim — a transient
#     retry must not create a second sandbox) or a different token (superseded).
#   * On dispatch FAILURE: ``e2b_dispatch_release_fenced`` DELs ONLY if the value
#     still equals our own token, so a superseded original cannot delete the
#     successor's key.
#   * On success the key is kept until the run is terminal: ``mark_complete``
#     calls ``e2b_dispatch_release_terminal`` (DEL) and the sandbox teardown in
#     the node runner releases it too. Upper TTL bound ~8h
#     (``E2B_IDEMPOTENCY_TTL_SECONDS``, >= timeout*retries + margin).
# ---------------------------------------------------------------------------


def e2b_idempotency_enabled() -> bool:
    """Return whether the SAQ E2B idempotency fence is enabled (settings knob)."""
    return bool(get_settings().saq_e2b_idempotency)


async def _e2b_client() -> Any:
    """Create a short-lived Redis client for one fence operation.

    Per-call client (opened and closed within the operation) matches the
    codebase pattern (dispatch.py / cron_helpers.py) and avoids leaking a
    process-lifetime connection pool into tests and worker teardown.
    """
    from redis.asyncio import Redis as _AsyncRedis

    settings = get_settings()
    return _AsyncRedis.from_url(
        settings.redis_url,
        socket_connect_timeout=10,
        socket_keepalive=True,
    )


def _e2b_key(run_id: str) -> str:
    return f"run:{run_id}:e2b"


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def e2b_dispatch_acquire(run_id: str, claim_token: str) -> None:
    """SETNX-before-dispatch for an E2B sandbox (plan F3a).

    Exactly one executor wins the run-level dispatch lock. Raises
    :class:`E2BIdempotencyDeniedError` (and does NOT create a sandbox) when the key
    already exists — whether held by a live dispatch from the same claim
    (transient retry) or by a superseded/different claim.
    """
    redis = await _e2b_client()
    try:
        key = _e2b_key(run_id)
        won = await redis.set(key, claim_token, nx=True, ex=E2B_IDEMPOTENCY_TTL_SECONDS)
        if won:
            return
        current = _coerce_str(await redis.get(key))
        if current == claim_token:
            raise E2BIdempotencyDeniedError(f"run {run_id}: same-claim E2B dispatch already live")
        raise E2BIdempotencyDeniedError(f"run {run_id}: E2B dispatch superseded by a different claim")
    finally:
        await redis.aclose()


async def e2b_dispatch_release_fenced(run_id: str, claim_token: str) -> None:
    """Fenced release on dispatch FAILURE — DEL only if the value is our token."""
    redis = await _e2b_client()
    try:
        key = _e2b_key(run_id)
        current = _coerce_str(await redis.get(key))
        if current == claim_token:
            await redis.delete(key)
    finally:
        await redis.aclose()


async def e2b_dispatch_release_terminal(run_id: str) -> None:
    """Terminal DEL — the run finished, the dispatch lock is released."""
    redis = await _e2b_client()
    try:
        await redis.delete(_e2b_key(run_id))
    finally:
        await redis.aclose()
