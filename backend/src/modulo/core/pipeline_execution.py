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
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.errors import NodeCancelledError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.run import get_run

_log = logging.getLogger(__name__)

# Claim staleness gates (configurable via settings).
RUN_CLAIM_STALE_SECONDS = 450

# RLS org-context SQL applied at the start of each run transaction (S1192).
_SQL_SET_ORG_ID = "SELECT set_config('app.organisation_id', :val, true)"

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
#   - ``executor_failed``: the executor task raised a generic exception in
#     ``run_executor_with_watchdog`` — the run is terminal-failed (token-guarded)
#     instead of silently completing.
#   - ``executor_heartbeat_lost``: the heartbeat loop failed fail-closed (3
#     consecutive DB/network failures) — the run is cancelled, the sandbox is
#     killed by id, and the run terminal-failed (token-guarded).
EXECUTOR_SETUP_FAILED_ERROR_CODE = "executor_setup_failed"
EXECUTOR_STALLED_ERROR_CODE = "executor_stalled"
EXECUTOR_FAILED_ERROR_CODE = "executor_failed"
EXECUTOR_HEARTBEAT_LOST_ERROR_CODE = "executor_heartbeat_lost"
# FAR-369 (defense-in-depth): a node started executing but did not COMPLETE
# within its configured ``timeout_seconds`` — independent of idle/activity.
# Distinct from EXECUTOR_STALLED_ERROR_CODE (the short setup-grace,
# claimed-but-nodeless zombie) so analytics/alerting can tell a half-alive
# stalled node from a run that never dispatched a node at all.
NODE_DEADLINE_EXCEEDED_ERROR_CODE = "node_deadline_exceeded"


class ClaimSupersededError(Exception):
    """Raised when this executor's claim token no longer matches the run's current claim.

    Signals a superseded executor (a successor re-claimed the run after an
    event-loop stall) so it aborts before overwriting the successor's state.
    """


def get_settings() -> Any:
    from modulo.settings import get_settings as _get_settings

    return _get_settings()


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
    _stale_seconds: int,
    _claim_cap: int | None = None,
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
                text(_SQL_SET_ORG_ID),
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
            # LIVE-BUG FIX (C3): the claim UPDATE runs on a raw connection — the
            # RLS policy ``organisation_id = current_setting('app.organisation_id')``
            # matches ZERO rows unless the org context is set on this connection
            # first. Under a NOBYPASSRLS role the claim silently returned None.
            await c.execute(
                text(_SQL_SET_ORG_ID),
                {"val": org_id},
            )
            result = await c.execute(
                build_claim_update(_stale_seconds=window, _claim_cap=cap, claim_token=claim_token),
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
    """Set the RLS org context for the session (transaction-scoped on Postgres).

    Delegates to the canonical helper in ``modulo.db.rls`` so the tenant-filter
    key (``org_id``) stays consistent with every other writer. Previously this
    copy wrote ``session.info["organisation_id"]`` which never activated the
    generic-backend tenant filter (the listener reads ``org_id``), leaving the
    run-by-id reads below unscoped.
    """
    from modulo.db.rls import set_rls_org as _canonical_set_rls_org

    await _canonical_set_rls_org(session, org_id)


async def load_and_setup(aeng: AsyncEngine, rid: uuid.UUID, oid: uuid.UUID) -> tuple[Any, Any]:
    """Load the Run and create a PipelineExecutor with checkpointer.

    Returns ``(run, executor)`` or ``(None, None)`` if the run is missing.
    """
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    factory = async_sessionmaker(aeng, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, oid)
        cur = await get_run(session, rid)
        if cur is None:
            _log.warning("Run %s not found during load", rid)
            return None, None

    settings = get_settings()
    conn_string = str(settings.database_url).replace("+asyncpg", "").replace("+psycopg", "")
    from modulo.core.notifier import Notifier

    notifier: Notifier | None = None
    try:
        notifier = Notifier(aeng, settings.fernet_key)
    except Exception:
        _log.exception("pipeline_execution.load_and_setup: notifier init failed — run still executes")
    executor = PipelineExecutor(aeng, checkpointer_conn_string=conn_string, notifier=notifier)
    return cur, executor


async def _read_current_claim_token(aeng: AsyncEngine, run_id: str, org_id: str) -> str | None:
    """Read the run's current ``claim_token`` from the DB (RLS-scoped)."""
    async with aeng.connect() as c:
        await c.execute(
            text(_SQL_SET_ORG_ID),
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

    When *claim_token* is provided the write is ATOMICALLY fenced: a single
    ``UPDATE runs SET heartbeat_at=now() WHERE id=:rid AND claim_token=:tok``
    (no read-then-compare window). Rowcount 0 means the run was superseded
    (token rotated by a successor) or the row is gone — raises
    :class:`ClaimSupersededError` so the caller aborts. ``job.update()`` is
    only called when the write actually landed (rowcount > 0), so a superseded
    original never touches the successor's job hash.
    """
    updated = False
    async with aeng.connect() as c:
        await c.execute(
            text(_SQL_SET_ORG_ID),
            {"val": org_id},
        )
        if claim_token is not None:
            result = await c.execute(
                text("UPDATE runs SET heartbeat_at=now() WHERE id=:rid AND claim_token=:tok RETURNING id"),
                {"rid": run_id, "tok": claim_token},
            )
            updated = result.fetchone() is not None
        else:
            await c.execute(
                text("UPDATE runs SET heartbeat_at=now() WHERE id=:rid"),
                {"rid": run_id},
            )
            updated = True
        await c.commit()
    if updated and job is not None:
        await job.update()
    if claim_token is not None and not updated:
        raise ClaimSupersededError(f"claim token superseded for run {run_id}")


async def heartbeat_loop(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    interval_seconds: int | None = None,
    job: Any = None,
    claim_token: str | None = None,
    superseded: asyncio.Event | None = None,
    health_failed: asyncio.Event | None = None,
) -> None:
    """Periodic heartbeat every ``RUN_HEARTBEAT_SECONDS`` to keep the run alive.

    The executor's claim token is captured at loop start (the claim just wrote
    it) and used to fence every heartbeat (plan F3a). When the run is
    superseded the heartbeat UPDATE matches zero rows and raises
    :class:`ClaimSupersededError`: the loop sets *superseded* (if provided) and
    breaks so ``run_executor_with_watchdog`` aborts the executor.

    Fail-closed health: consecutive DB/network failures (exceptions) are
    counted; after 3 in a row *health_failed* is set (if provided) and the loop
    breaks so the wrapper can kill the sandbox and terminal-fail the run with
    ``executor_heartbeat_lost``.
    """
    if interval_seconds is None:
        interval_seconds = get_settings().run_heartbeat_seconds
    if claim_token is None:
        claim_token = await _read_current_claim_token(aeng, run_id, org_id)
    consecutive_failures = 0
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await heartbeat_once(aeng, run_id, org_id, job=job, claim_token=claim_token)
            consecutive_failures = 0
        except ClaimSupersededError:
            _log.warning("Heartbeat superseded for run %s — aborting heartbeat", run_id)
            if superseded is not None:
                superseded.set()
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures = _heartbeat_failure(consecutive_failures, health_failed, run_id)
            if consecutive_failures >= 3:
                break


def _heartbeat_failure(
    consecutive_failures: int,
    health_failed: asyncio.Event | None,
    run_id: str,
) -> int:
    """Count a heartbeat failure and fail closed after three in a row.

    Increments the consecutive-failure counter, logs it, and — once the
    threshold is reached — logs the fatal condition and signals *health_failed*
    (if provided) so the enclosing wrapper can kill the sandbox and
    terminal-fail the run with ``executor_heartbeat_lost``. Returns the updated
    counter for the caller's break decision.
    """
    consecutive_failures += 1
    _log.warning("Heartbeat failed for run %s (%d consecutive)", run_id, consecutive_failures)
    if consecutive_failures >= 3:
        _log.error(
            "Heartbeat lost for run %s after %d consecutive failures — failing closed",
            run_id,
            consecutive_failures,
        )
        if health_failed is not None:
            health_failed.set()
    return consecutive_failures


async def mark_complete(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    claim_token: str | None = None,
) -> None:
    """Mark a still-running run complete using the DB enum value ('complete').

    Idempotent and ATOMICALLY fenced: a single conditional
    ``UPDATE ... SET status='complete', completed_at=now() WHERE status='running'
    AND cancellation_requested=false AND (:tok IS NULL OR claim_token=:tok)``
    (no read-then-write window). Rowcount 0 means the run was superseded (a
    successor rotated the token), cancelled, or already terminal — the write is
    skipped with a warning and the caller proceeds as a no-op.
    """
    async with aeng.connect() as c, c.begin():
        await c.execute(
            text(_SQL_SET_ORG_ID),
            {"val": org_id},
        )
        result = await c.execute(
            text(
                "UPDATE runs SET status='complete', completed_at=now() "
                "WHERE id=:rid AND status='running' AND cancellation_requested = false "
                "AND (CAST(:tok AS text) IS NULL OR claim_token = CAST(:tok AS text)) "
                "RETURNING id"
            ),
            {"rid": run_id, "tok": claim_token},
        )
        completed = result.fetchone() is not None
    if not completed:
        _log.warning(
            "mark_complete skipped for run %s (claim superseded, cancelled, or not running)",
            run_id,
        )
    else:
        # FAR-143 — a raw terminal write never runs finalize_cost, so journeys
        # would never advance. Advance from the run's CREATE-STAMPED refs (no
        # self-report parse here — there are no merged outputs).
        await _advance_journeys_from_stored_refs(aeng, run_id, org_id, RUN_COMPLETE_STATUS)


async def fail_run_terminal(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    error_code: str,
    error_detail: str,
    claim_token: str | None = None,
) -> bool:
    """Terminal-fail a claimed-but-stuck run (zombie protection).

    ATOMICALLY fenced: only transitions a run that is currently ``running`` and
    whose claim token still matches this executor's (when *claim_token* is
    given), with ``cancellation_requested = false`` (CANCEL-WINS). Returns
    ``False`` when the guards reject the write (already terminal, superseded,
    capacity-deferred back to ``pending``, or cancelled) — a superseded
    original cannot fail the run out from under a successor.
    """
    async with aeng.connect() as c, c.begin():
        await c.execute(
            text(_SQL_SET_ORG_ID),
            {"val": org_id},
        )
        result = await c.execute(
            text(
                "UPDATE runs SET status='failed', error_code=:code, error_detail=:detail, completed_at=now() "
                "WHERE id=:rid AND status='running' AND cancellation_requested = false "
                "AND (CAST(:tok AS text) IS NULL OR claim_token = CAST(:tok AS text)) "
                "RETURNING id"
            ),
            {
                "rid": run_id,
                "code": error_code,
                "detail": error_detail[:5000],
                "tok": claim_token,
            },
        )
        ok = result.fetchone() is not None
    if ok:
        _log.warning(
            "run.terminal_failed run=%s error_code=%s",
            run_id,
            error_code,
        )
        # FAR-143 — same raw-writer gap as mark_complete: advance journeys from
        # the run's CREATE-STAMPED refs (fail-open).
        await _advance_journeys_from_stored_refs(aeng, run_id, org_id, "failed")
        # FAR-162 — compensating analytics fact for the raw terminal failure
        # (separate session, fail-open). Idempotent vs a later finalize write.
        # Call-site guard keeps a best-effort facts regression from ever
        # surfacing out of the terminal-fail path (the helper is also fail-open).
        try:
            await _record_fact_for_terminal_failed_run(aeng, run_id, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("pipeline_execution.terminal_failed_facts_failed run=%s", run_id, exc_info=True)
    return ok


async def _advance_journeys_from_stored_refs(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    status: str,
) -> None:
    """FAR-143 — advance journeys from a run's stored refs, fail-open.

    ``mark_complete`` / ``fail_run_terminal`` write the terminal status with a
    raw ``text()`` UPDATE on a connection and never run ``finalize_cost`` (so
    they never parse outputs or persist them). Runs carrying CREATE-STAMPED
    refs (``runs.work_item_refs``) would therefore never advance their journeys
    through those paths. This helper opens its OWN session/transaction AFTER the
    raw write succeeds (the write is committed before it runs) and advances
    journeys from the stored refs only — no self-report parse here (the raw
    writers have no merged outputs).

    FAIL-OPEN: a journey-write failure is logged and swallowed — it must never
    roll back or fail the already-committed terminal write.
    """
    try:
        from modulo.core.lifecycle_map.advancement import advance_journeys

        factory = async_sessionmaker(aeng, expire_on_commit=False, autobegin=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, uuid.UUID(org_id))
            run = await get_run(session, uuid.UUID(run_id))
            if run is None or not run.work_item_refs:
                return
            await advance_journeys(
                session,
                run.organisation_id,
                run_id=run.id,
                pipeline_id=run.pipeline_id,
                refs=run.work_item_refs,
                status=status,
                completed_at=run.completed_at,
                run_created_at=run.created_at,
                is_replay=bool(run.is_replay),
                variant_group_id=run.variant_group_id,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("pipeline_execution.journey_advance_failed run=%s", run_id, exc_info=True)


async def _record_fact_for_terminal_failed_run(aengine: AsyncEngine, run_id: str, org_id: str) -> None:
    """Best-effort daily-fact write for a run terminalised by a raw writer (P6').

    ``fail_run_terminal`` / the stale-run sweep write ``status='failed'`` with
    a raw ``text()`` UPDATE on a connection and never run ``finalize_cost``, so
    those runs would never appear in ``run_daily_facts`` (invisible in the
    analytics failure/stall dimensions). This helper opens its OWN session/
    transaction AFTER the raw terminal UPDATE commits, sets the RLS org
    context, re-selects the Run ORM (a pre-update entity would record
    ``status='running'`` with a NULL ``completed_at``), and records the daily
    fact via the shared :func:`record_fact_for_terminal_failed_run` wrapper.
    None-guarded and fail-open: any failure logs and is swallowed — it must
    never roll back or fail the already-committed terminal write.
    """
    try:
        from modulo.core.analytics import record_fact_for_terminal_failed_run

        factory = async_sessionmaker(aengine, expire_on_commit=False, autobegin=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, uuid.UUID(org_id))
            run = await get_run(session, uuid.UUID(run_id))
            if run is None:
                _log.warning("pipeline_execution.terminal_failed_facts_run_missing run=%s", run_id)
                return
            await record_fact_for_terminal_failed_run(session, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("pipeline_execution.terminal_failed_facts_failed run=%s", run_id, exc_info=True)


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


async def _await_first_node(
    node_started_event: asyncio.Event,
    run_done_event: asyncio.Event,
    exec_task: asyncio.Task[Any],
) -> bool:
    """Wait for a node to start, the run to finish, or the executor to finish.

    Blocks until a node starts (``node_started_event``), the run becomes
    terminal (``run_done_event``), or the executor task completes. Returns
    ``True`` to continue the watchdog loop, ``False`` to stand down (the run
    finished or the executor finished). Clears ``node_started_event`` when a
    node started so the caller can recompute the in-flight deadline set.
    """
    started_wait = asyncio.ensure_future(node_started_event.wait())
    done_wait = asyncio.ensure_future(run_done_event.wait())
    try:
        await asyncio.wait({started_wait, done_wait, exec_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        started_wait.cancel()
        done_wait.cancel()
    if run_done_event.is_set() or exec_task.done():
        return False
    if node_started_event.is_set():
        node_started_event.clear()
    return True


async def _await_progress(
    node_completed_event: asyncio.Event,
    node_started_event: asyncio.Event,
    run_done_event: asyncio.Event,
    exec_task: asyncio.Task[Any],
    remaining: float,
) -> bool:
    """Wait for a node completion/start, run finish, executor finish, or deadline.

    Uses the nearest in-flight deadline (``remaining``) as the wait bound so the
    watchdog never blocks past its hard deadline. Clears the single-shot
    wake-up events so the next iteration re-arms them. Returns ``True`` to keep
    looping, ``False`` to stand down (run finished / executor finished).
    """
    c = asyncio.ensure_future(node_completed_event.wait())
    s = asyncio.ensure_future(node_started_event.wait())
    d = asyncio.ensure_future(run_done_event.wait())
    try:
        await asyncio.wait({c, s, d, exec_task}, return_when=asyncio.FIRST_COMPLETED, timeout=remaining)
    finally:
        c.cancel()
        s.cancel()
        d.cancel()
    node_started_event.clear()
    node_completed_event.clear()
    return not (run_done_event.is_set() or exec_task.done())


async def _fail_overdue_node(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    node_deadlines: dict[str, tuple[float, int]],
    exec_task: asyncio.Task[Any],
    run_done_event: asyncio.Event,
    stall_requested: asyncio.Event | None,
) -> None:
    """Fail the most-overdue in-flight node that blew its deadline.

    Cancels ``exec_task`` FIRST, signals ``stall_requested`` (so the wrapper
    distinguishes a watchdog-initiated cancellation from worker shutdown), then
    terminal-fails the run with ``node_deadline_exceeded``. Does nothing when
    the run finished or the executor finished concurrently (stands down so it
    never double-fails an already-finished run).
    """
    exceeded = [nid for nid, (dl, _to) in node_deadlines.items() if dl <= time.monotonic()]
    exceeded.sort(key=lambda nid: node_deadlines[nid][0])
    node_id = exceeded[0]
    _timeout = node_deadlines[node_id][1]
    if run_done_event.is_set() or exec_task.done():
        return
    _log.warning(
        "node_deadline_watchdog.exceeded run=%s node=%s exceeded timeout_seconds=%ds — "
        "cancelling executor and failing run",
        run_id,
        node_id,
        _timeout,
    )
    exec_task.cancel()
    if stall_requested is not None:
        stall_requested.set()
    await fail_run_terminal(
        aeng,
        run_id,
        org_id,
        error_code=NODE_DEADLINE_EXCEEDED_ERROR_CODE,
        error_detail=(
            f"Node '{node_id}' did not complete within its configured timeout_seconds "
            f"({_timeout}s) — absolute node-deadline watchdog (half-alive stall)"
        ),
    )


async def node_deadline_watchdog(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    exec_task: asyncio.Task[Any],
    stall_requested: asyncio.Event | None = None,
    node_started_event: asyncio.Event,
    node_completed_event: asyncio.Event,
    run_done_event: asyncio.Event,
    node_deadlines: dict[str, tuple[float, int]],
    default_timeout: int | None = None,
) -> None:
    """Fail a node that does not COMPLETE within its configured ``timeout_seconds``.

    Defense-in-depth for the sandbox_agent half-alive stall (FAR-369): when
    opencode awaits a "continue" prompt that never comes but keeps the SSE
    connection half-alive (comment heartbeats reset ``chunkTimeout``), the
    idle-watchdog never fires (the node is never "idle") and the run would hang
    until the 35-min nodeless-zombie backstop (``agent.stall``). This watchdog
    holds each node to its OWN hard deadline — measured from when the node
    STARTED executing, independent of idle/activity — and fails the run the
    instant the deadline passes, instead of riding to 35 min.

    It complements (does NOT replace) the idle-watchdog: that one catches truly
    idle stalls fast; this one catches half-alive stalls at the node's hard
    deadline. It also complements the short setup-grace ``zombie_watchdog``:
    that one bounds the pre-node window (no node dispatched); this one bounds a
    node that DID dispatch but never finished.

    Tracks EVERY in-flight node, not just the single most-recent one. LangGraph
    runs sibling branches concurrently within a shared superstep (parallel
    fan-out — a supported, first-class topology), so a single ``current_node``
    pointer would let a stalled sibling hide behind a newer sibling's start:
    when sibling B's ``on_chain_start`` lands mid-flight while stalled A is
    still executing, the watchdog would repoint to B and abandon A's deadline,
    leaving A to ride to the 35-min backstop. ``node_deadlines`` maps
    ``node_id -> (absolute_deadline, timeout_seconds)`` and is populated by the
    executor's per-node callbacks (``on_node_started`` adds an entry with its
    deadline; ``on_node_completed`` removes it). The watchdog enforces each
    entry's deadline independently, so a stalled A is failed even if B starts
    and completes alongside it.

    Coordination mirrors :func:`zombie_watchdog`: cancel ``exec_task`` FIRST,
    then signal ``stall_requested`` (so the wrapper distinguishes a
    watchdog-initiated cancellation from a worker shutdown), then terminal-fail
    the run with ``node_deadline_exceeded``. Cancelling the executor FIRST
    ensures a late-returning ``execute`` cannot overwrite the failure through
    ``finalize_cost``. The wrapper awaits this task to completion before
    cancelling it (see the ``exec_task.done()`` wait below, which lets the
    watchdog stand down promptly without a mid-transaction cancel), so a pending
    ``fail_run_terminal`` transaction commits.

    Stands down (no-op) if: the run completes / becomes terminal before any
    deadline, every in-flight node completes within its ``timeout_seconds``, the
    executor task finishes, or all in-flight nodes complete — so it never fails
    an already-finished run and never double-fails with the idle-watchdog or the
    35-min backstop.
    """
    if default_timeout is None:
        default_timeout = int(get_settings().saq_node_default_timeout_seconds)

    while True:
        # Stand down if the run is already over or the executor finished. We
        # also wait on exec_task.done() (below) so that when the wrapper cancels
        # exec_task (watchdog stall / supersession / heartbeat loss) this task
        # stands down cleanly instead of blocking on node_started_event.
        if run_done_event.is_set() or exec_task.done():
            return
        # Advance one watchdog step: either wait for a fresh in-flight node to
        # appear, or enforce the soonest in-flight deadline. ``node_deadlines``
        # is the source of truth for in-flight nodes (populated by
        # on_node_started / on_node_completed), so a parallel fan-out merely
        # adds a second entry rather than replacing the first. ``False`` means
        # the run finished / the executor finished / the deadline blew — stand
        # down.
        if not await _node_deadline_step(
            aeng,
            run_id,
            org_id,
            exec_task=exec_task,
            stall_requested=stall_requested,
            node_started_event=node_started_event,
            node_completed_event=node_completed_event,
            run_done_event=run_done_event,
            node_deadlines=node_deadlines,
        ):
            return


async def _node_deadline_step(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    *,
    exec_task: asyncio.Task[Any],
    stall_requested: asyncio.Event | None,
    node_started_event: asyncio.Event,
    node_completed_event: asyncio.Event,
    run_done_event: asyncio.Event,
    node_deadlines: dict[str, tuple[float, int]],
) -> bool:
    """Advance the node-deadline watchdog by one step; ``False`` to stand down.

    When at least one node is in flight, enforce EACH entry's own deadline —
    parallel fan-out means multiple siblings may be running at once, and a
    stalled one must be failed independently of its siblings' progress. When
    no node is in flight, wait for one to start executing OR the run to finish —
    so we never block forever after the last node completes. Returns ``True``
    to keep looping.
    """
    if node_deadlines:
        soonest_deadline = min(dl for dl, _to in node_deadlines.values())
        remaining = soonest_deadline - time.monotonic()
        if remaining <= 0:
            await _fail_overdue_node(aeng, run_id, org_id, node_deadlines, exec_task, run_done_event, stall_requested)
            return False
        # Wait until a node completes, a new node starts, the run finishes, the
        # executor is cancelled, or the soonest deadline elapses — whichever
        # comes first, then recompute on the next loop iteration.
        return await _await_progress(node_completed_event, node_started_event, run_done_event, exec_task, remaining)
    return await _await_first_node(node_started_event, run_done_event, exec_task)


async def _read_run_status(aeng: AsyncEngine, run_id: str, org_id: str) -> str | None:
    """Read the run's current status (RLS-scoped)."""
    async with aeng.connect() as c:
        await c.execute(
            text(_SQL_SET_ORG_ID),
            {"val": org_id},
        )
        result = await c.execute(text("SELECT status FROM runs WHERE id=:rid"), {"rid": run_id})
        row = result.first()
        return str(row[0]) if row and row[0] else None


async def _kill_sandbox_best_effort(aeng: AsyncEngine, run_id: str, org_id: str) -> None:
    """Best-effort kill of the run's recorded E2B sandbox (by id).

    Reads ``runs.sandbox_id`` via a session and calls the E2B SDK kill on that
    sandbox. Never raises — a failed kill is logged and the caller proceeds.
    """
    try:
        sandbox_id: str | None = None
        async with aeng.connect() as c:
            await c.execute(
                text(_SQL_SET_ORG_ID),
                {"val": org_id},
            )
            result = await c.execute(text("SELECT sandbox_id FROM runs WHERE id=:rid"), {"rid": run_id})
            row = result.first()
            sandbox_id = str(row[0]) if row and row[0] else None
        if not sandbox_id:
            return
        from e2b import AsyncSandbox

        sandbox = await asyncio.wait_for(AsyncSandbox.connect(sandbox_id), timeout=10.0)
        await asyncio.wait_for(sandbox.kill(request_timeout=10.0), timeout=10.0)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("pipeline_execution.sandbox_kill_failed run=%s", run_id, exc_info=True)


async def run_executor_with_watchdog(
    aeng: AsyncEngine,
    *,
    run_id: str,
    org_id: str,
    executor: Any,
    job: Any,
    execute_fn: Callable[[], Awaitable[Any]],
    claim_token: str | None = None,
    superseded: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Run ``execute_fn`` under the DB heartbeat loop + zombie watchdog.

    Shared by ``saq_worker.execute_run`` and ``resume_run``. Expected flow:

    * The caller has already claimed the run (``status='running'``) and loaded
      the executor via :func:`load_and_setup`.
    * The executor must expose ``on_first_progress`` (a no-arg callable); this
      helper wires it to an :class:`asyncio.Event` that the zombie watchdog
      waits on. The executor calls it when the first node dispatches.
    * The heartbeat loop starts concurrently (as today) and keeps the run alive
      during legitimate node execution. It is fenced by *claim_token*; when a
      successor re-claims the run the loop sets *superseded* and the executor is
      aborted. When 3 consecutive heartbeat writes fail it sets a
      fail-closed event: the sandbox is killed by id and the run is
      terminal-failed with ``executor_heartbeat_lost``.
    * If no progress is signalled within ``SAQ_SETUP_GRACE_SECONDS`` the
      watchdog cancels the executor and fails the run (``executor_stalled``).
    * An ``asyncio.CancelledError`` raised by the executor task is swallowed
      ONLY when the watchdog (``stall_requested``), a supersession
      (``superseded``), or heartbeat loss caused it; a genuine worker shutdown
      cancellation re-raises. All three await the watchdog to completion first
      so a pending ``fail_run_terminal`` transaction commits.

    Honest outcomes: returns ``{"status": "complete"}`` only when the run row
    actually reached ``complete``; a generic executor exception is
    terminal-failed with ``executor_failed`` (token-guarded) and returns
    ``{"status": "failed"}``; an ``awaiting_human`` pause returns
    ``{"status": "awaiting_human"}``. The caller only runs ``mark_complete`` on
    a genuine ``complete``.
    """
    rid = uuid.UUID(run_id)

    first_progress = asyncio.Event()
    stall_requested = asyncio.Event()
    health_failed = asyncio.Event()
    # FAR-369 absolute node-deadline watchdog state. ``node_started_event`` /
    # ``node_completed_event`` are pulsed by the executor's per-node callbacks
    # (wired below); ``node_deadlines`` maps each in-flight node_id to its
    # (absolute_deadline, timeout_seconds) — populated per on_node_started and
    # removed per on_node_completed — so parallel fan-out siblings are tracked
    # independently; ``run_done_event`` is set once the run is terminal/complete
    # so the watchdog stands down instead of failing an already-finished run.
    node_started_event = asyncio.Event()
    node_completed_event = asyncio.Event()
    run_done_event = asyncio.Event()
    node_deadlines: dict[str, tuple[float, int]] = {}
    if superseded is None:
        superseded = asyncio.Event()
    if executor is not None:
        executor.on_first_progress = first_progress.set

        # FAR-369: wire the per-node start/completion callbacks that drive the
        # absolute node-deadline watchdog. Each node gets its own deadline entry
        # (set on start, removed on completion) so a stalled sibling in a
        # parallel fan-out cannot hide behind a newer sibling's start.
        def _on_node_started(nid: str) -> None:
            to = node_timeouts.get(nid, default_timeout)
            node_deadlines[nid] = (time.monotonic() + to, to)
            node_started_event.set()

        def _on_node_completed(nid: str) -> None:
            node_deadlines.pop(nid, None)
            node_completed_event.set()

        executor.on_node_started = _on_node_started
        executor.on_node_completed = _on_node_completed
        # Wire the cancellation-intent signals into the executor so its
        # NodeCancelledError retry handler can distinguish a watchdog stall
        # from a supersession and skip the pending-reset when either fired.
        executor._stall_requested = stall_requested
        executor._superseded = superseded

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
    # FAR-369 absolute node-deadline watchdog: fails a node that does not
    # COMPLETE within its configured timeout_seconds, independent of
    # idle/activity. Complements the idle-watchdog (truly-idle stalls) and the
    # short setup-grace zombie_watchdog (nodeless zombies). Shares exec_task /
    # stall_requested coordination with zombie_watchdog. node_timeouts is read
    # from the executor (populated from the graph JSON before streaming) as a
    # shared dict reference so it is filled by the time the first node starts.
    # Read the live dict reference (populated from the graph JSON by
    # _prepare_and_stream before streaming). Pass the same object — NOT a copy
    # — so the watchdog sees the per-node timeouts once they are filled in.
    node_timeouts = executor._node_timeouts if executor is not None else {}
    default_timeout = get_settings().saq_node_default_timeout_seconds
    node_deadline_task = asyncio.create_task(
        node_deadline_watchdog(
            aeng,
            run_id,
            org_id,
            exec_task=exec_task,
            stall_requested=stall_requested,
            node_started_event=node_started_event,
            node_completed_event=node_completed_event,
            run_done_event=run_done_event,
            node_deadlines=node_deadlines,
            default_timeout=default_timeout,
        ),
        name=f"saq-node-deadline-watchdog-{rid}",
    )
    heartbeat_task = asyncio.create_task(  # nosemgrep: create-task-without-guard
        heartbeat_loop(
            aeng,
            run_id,
            org_id,
            job=job,
            claim_token=claim_token,
            superseded=superseded,
            health_failed=health_failed,
        ),
        name=f"saq-heartbeat-{rid}",
    )

    async def _abort_watcher() -> None:
        waiters = [asyncio.create_task(superseded.wait()), asyncio.create_task(health_failed.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        if not exec_task.done():
            exec_task.cancel()
        if not watchdog_task.done():
            watchdog_task.cancel()
        if not node_deadline_task.done():
            node_deadline_task.cancel()

    abort_watch_task = asyncio.create_task(_abort_watcher(), name=f"saq-abort-watch-{rid}")

    exec_exc, exec_result = await _await_executor_task(
        aeng=aeng,
        run_id=run_id,
        org_id=org_id,
        claim_token=claim_token,
        rid=rid,
        exec_task=exec_task,
        run_done_event=run_done_event,
        watchdog_task=watchdog_task,
        node_deadline_task=node_deadline_task,
        heartbeat_task=heartbeat_task,
        abort_watch_task=abort_watch_task,
        stall_requested=stall_requested,
        health_failed=health_failed,
        superseded=superseded,
    )

    if exec_exc is not None:
        # Honest outcome: a generic executor failure is terminal-failed
        # (token-guarded) BEFORE returning — never a silent wrong-success.
        await fail_run_terminal(
            aeng,
            run_id,
            org_id,
            error_code=EXECUTOR_FAILED_ERROR_CODE,
            error_detail=f"{type(exec_exc).__name__}: {exec_exc}"[:2000],
            claim_token=claim_token,
        )
        return {"status": "failed"}

    # Derive the outcome from the executor task result (the returned Run row)
    # and the run row status. A watchdog/supersession/heartbeat cancellation
    # leaves exec_result None — fall back to the run row.
    result_status = await _resolve_result_status(aeng, run_id, org_id, exec_result, rid)
    if result_status == "complete":
        return {"status": "complete"}
    if result_status == "awaiting_human":
        return {"status": "awaiting_human"}
    return {"status": "failed"}


async def _await_executor_task(
    *,
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    claim_token: str | None,
    rid: uuid.UUID,
    exec_task: asyncio.Task[Any],
    run_done_event: asyncio.Event,
    watchdog_task: asyncio.Task[Any] | None,
    node_deadline_task: asyncio.Task[Any],
    heartbeat_task: asyncio.Task[Any] | None,
    abort_watch_task: asyncio.Task[Any] | None,
    stall_requested: asyncio.Event,
    health_failed: asyncio.Event,
    superseded: asyncio.Event,
) -> tuple[Exception | None, Any]:
    """Await the executor task and classify its outcome.

    Returns ``(exc, result)``: ``(None, result)`` on a clean completion,
    ``(exc, None)`` on a generic executor failure (terminal-failed by the
    caller). A watchdog / supersession / heartbeat-loss cancellation is
    classified via :func:`_resolve_cancel_outcome` (which re-raises for a
    genuine worker-shutdown cancellation) and returns ``(None, None)`` so the
    caller falls back to the run row. A transient ``NodeCancelledError`` is
    re-raised so SAQ retries the job. The ``finally`` cancels and drains every
    helper task before the outcome is resolved.
    """
    try:
        result = await exec_task
        # The run reached a terminal/complete outcome via the executor — signal
        # the node-deadline watchdog to stand down (never fail a finished run).
        run_done_event.set()
        return None, result
    except asyncio.CancelledError:
        # Heartbeat-loss, watchdog stall, or supersession. Await the watchdogs
        # to completion first so their ``fail_run_terminal`` transactions commit
        # before the ``finally`` below cancels them — cancelling a watchdog
        # mid-write would abort its terminal-fail transaction and leave the run
        # ``running`` forever.
        await _resolve_cancel_outcome(
            watchdog_task=watchdog_task,
            node_deadline_task=node_deadline_task,
            stall_requested=stall_requested,
            health_failed=health_failed,
            superseded=superseded,
            aeng=aeng,
            run_id=run_id,
            org_id=org_id,
            claim_token=claim_token,
            rid=rid,
        )
        return None, None
    except NodeCancelledError:
        # Transient node cancellation — execute() already reset the run to
        # pending; re-raise so SAQ retries the job.
        raise
    except Exception as exc:
        # A generic executor failure will be terminal-failed by the caller —
        # signal the node-deadline watchdog to stand down so it does not
        # double-fail.
        run_done_event.set()
        _log.exception("run_executor_with_watchdog: execute failed for run %s", rid)
        return exc, None
    finally:
        await _cancel_and_await_tasks(abort_watch_task, watchdog_task, node_deadline_task, exec_task, heartbeat_task)


async def _resolve_cancel_outcome(
    *,
    watchdog_task: asyncio.Task[Any] | None,
    node_deadline_task: asyncio.Task[Any],
    stall_requested: asyncio.Event,
    health_failed: asyncio.Event,
    superseded: asyncio.Event,
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    claim_token: str | None,
    rid: uuid.UUID,
) -> None:
    """Classify a cancelled execution: watchdog / heartbeat / supersession.

    Awaits the in-flight watchdogs to completion FIRST so their
    ``fail_run_terminal`` transactions commit, then handles the abort cause:
    the node/executor watchdog (``stall_requested``), heartbeat loss
    (``health_failed`` — kill the sandbox and terminal-fail with
    ``executor_heartbeat_lost``), or a supersession (``superseded``). A
    genuine worker-shutdown cancellation is re-raised so SAQ can retry.
    """
    if watchdog_task is not None and not watchdog_task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
    if not node_deadline_task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await node_deadline_task
    if stall_requested.is_set():
        _log.warning("run_executor_with_watchdog: execution cancelled by node/executor watchdog for run %s", rid)
    elif health_failed.is_set():
        _log.error(
            "run_executor_with_watchdog: heartbeat lost for run %s — killing sandbox and failing run",
            rid,
        )
        await _kill_sandbox_best_effort(aeng, run_id, org_id)
        await fail_run_terminal(
            aeng,
            run_id,
            org_id,
            error_code=EXECUTOR_HEARTBEAT_LOST_ERROR_CODE,
            error_detail=(
                "Heartbeat loop failed fail-closed after 3 consecutive DB/network failures (executor_heartbeat_lost)"
            ),
            claim_token=claim_token,
        )
    elif superseded.is_set():
        _log.warning(
            "run_executor_with_watchdog: execution aborted for run %s (superseded by a newer claim)",
            rid,
        )
    else:
        raise asyncio.CancelledError


async def _cancel_and_await_tasks(
    abort_watch_task: asyncio.Task[Any] | None,
    watchdog_task: asyncio.Task[Any] | None,
    node_deadline_task: asyncio.Task[Any],
    exec_task: asyncio.Task[Any] | None,
    heartbeat_task: asyncio.Task[Any] | None,
) -> None:
    """Cancel and drain all helper tasks before returning (fail-safe)."""
    if abort_watch_task is not None:
        abort_watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await abort_watch_task
    if watchdog_task is not None:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
    node_deadline_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await node_deadline_task
    if exec_task is not None and not exec_task.done():
        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


async def _resolve_result_status(
    aeng: AsyncEngine,
    run_id: str,
    org_id: str,
    exec_result: Any,
    rid: uuid.UUID,
) -> str | None:
    """Resolve the run's effective status for the outcome decision.

    Prefers the status of the executor's returned Run row, falling back to a
    re-read of the row when the result is missing or still ``pending`` (a
    watchdog/supersession/heartbeat cancellation leaves ``exec_result`` None).
    Never raises: a row read failure logs and degrades to ``None``.
    """
    status = getattr(exec_result, "status", None)
    if status in (None, "pending"):
        try:
            status = await _read_run_status(aeng, run_id, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("run_executor_with_watchdog: could not read run status for %s", rid)
            status = None
    return status


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


async def _sweep_org_stale_runs(
    conn: Any,
    *,
    org_id: uuid.UUID,
    nd_window: int,
    wl_window: int,
    stranded_rows: list[Any],
    terminalised_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
) -> tuple[int, int, int]:
    """Run one org's stale-run UPDATE set; return (never, capacity_timeout, lost) counts.

    Runs inside the caller's RLS-scoped transaction and mutates ``stranded_rows``
    and ``terminalised_run_ids`` in place so the caller can post-process them once
    the transaction commits. Extracted from :func:`stale_run_recovery_sweep` to keep
    that function's control flow shallow.
    """
    await conn.execute(
        text(_SQL_SET_ORG_ID),
        {"val": str(org_id)},
    )
    never_result = await conn.execute(
        text(
            "UPDATE runs "
            "SET status = 'failed', error_code = 'never_dispatched', "
            "error_detail = :detail, completed_at = now() "
            "WHERE status = 'pending' "
            "AND organisation_id = :oid "
            "AND created_at < now() - (:nd_window * interval '1 second') "
            "AND dispatched_at IS NULL "
            "AND cancellation_requested = false "
            "AND (error_code IS NULL OR error_code NOT IN ('org_capacity_limited', 'pipeline_capacity')) "
            "AND (dispatcher IS NULL OR dispatcher != 'saq') "
            "RETURNING id"
        ),
        {
            "oid": str(org_id),
            "nd_window": nd_window,
            "detail": "Run was not dispatched within the stale threshold.",
        },
    )
    never_count = never_result.rowcount or 0
    terminalised_run_ids.extend((row[0], org_id) for row in never_result.all())

    stranded_result = await conn.execute(
        text(
            "UPDATE runs "
            "SET heartbeat_at = now() "
            "WHERE status = 'pending' "
            "AND organisation_id = :oid "
            "AND error_code IN ('org_capacity_limited', 'pipeline_capacity') "
            "AND (heartbeat_at IS NULL OR heartbeat_at < now() - (:redispatch_ttl * interval '1 minute')) "
            "AND created_at >= now() - (:fail_ttl * interval '1 minute') "
            "AND cancellation_requested = false "
            "RETURNING id, organisation_id"
        ),
        {
            "oid": str(org_id),
            "redispatch_ttl": _STRANDED_REDISPATCH_TTL_MINUTES,
            "fail_ttl": CAPACITY_TIMEOUT_TTL_MINUTES,
        },
    )
    stranded_rows.extend(stranded_result.all())

    capacity_timeout_result = await conn.execute(
        text(
            "UPDATE runs "
            "SET status = 'failed', error_code = 'capacity_timeout', "
            "error_detail = :detail, completed_at = now() "
            "WHERE status = 'pending' "
            "AND organisation_id = :oid "
            "AND error_code IN ('org_capacity_limited', 'pipeline_capacity') "
            "AND created_at < now() - (:ttl * interval '1 minute') "
            "AND cancellation_requested = false "
            "RETURNING id"
        ),
        {
            "oid": str(org_id),
            "ttl": CAPACITY_TIMEOUT_TTL_MINUTES,
            "detail": "Waited in capacity queue past the TTL.",
        },
    )
    capacity_timeout_count = capacity_timeout_result.rowcount or 0
    terminalised_run_ids.extend((row[0], org_id) for row in capacity_timeout_result.all())

    lost_result = await conn.execute(
        text(
            "UPDATE runs "
            "SET status = 'failed', error_code = 'worker_lost', "
            "error_detail = :detail, completed_at = now() "
            "WHERE status = 'running' "
            "AND organisation_id = :oid "
            "AND heartbeat_at < now() - (:wl_window * interval '1 second') "
            "AND claim_count >= 5 "
            "AND (dispatcher IS NULL OR dispatcher != 'saq') "
            "RETURNING id"
        ),
        {
            "oid": str(org_id),
            "wl_window": wl_window,
            "detail": "Worker lost heartbeat for this run.",
        },
    )
    lost_count = lost_result.rowcount or 0
    terminalised_run_ids.extend((row[0], org_id) for row in lost_result.all())
    return never_count, capacity_timeout_count, lost_count


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
    - Every run terminalised by the sweep (never_dispatched / capacity_timeout
      / worker_lost) has its journeys advanced from its CREATE-STAMPED refs
      (FAR-143 follow-up) — the raw UPDATEs never run ``finalize_cost``, so
      without this the swept runs' journeys would never move. Fail-open per
      run; stranded capacity-blocked runs are re-dispatched, never terminal,
      and so never advance.
    - Every run terminalised by the sweep also gets a compensating
      ``run_daily_facts`` row (FAR-162, P6'): the raw UPDATEs never run
      ``finalize_cost``, so without this the swept runs would be invisible to
      the analytics failure/stall dimensions. Written in its own separate
      RLS-scoped session after the sweep's UPDATEs commit; fail-open per run.

    The ``never_dispatched`` / ``worker_lost`` writers stamp a synthetic
    ``error_detail`` (FAR-164): the daily-watcher hang-death detector keys on
    ``error_code == 'node_cancelled'`` ONLY, so a string detail on these
    legacy-dispatch codes can never be miscounted as a hang death.

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
        # Runs terminalised to ``failed`` by this sweep — (run_id, org_id) —
        # whose journeys must advance once the UPDATEs commit (FAR-143 follow-up).
        terminalised_run_ids: list[tuple[uuid.UUID, uuid.UUID]] = []
        for org_id in org_ids:
            async with async_engine.connect() as conn, conn.begin():
                never_delta, capacity_delta, lost_delta = await _sweep_org_stale_runs(
                    conn,
                    org_id=org_id,
                    nd_window=nd_window,
                    wl_window=wl_window,
                    stranded_rows=stranded_rows,
                    terminalised_run_ids=terminalised_run_ids,
                )
                never_count += never_delta
                capacity_timeout_count += capacity_delta
                lost_count += lost_delta
        stranded_count = len(stranded_rows)

        # Re-dispatch AFTER each org's sweep transaction commits so dispatch_run's
        # own sessions (and the row lock the UPDATE held) never overlap a live
        # transaction.
        redispatch_outcomes: dict[str, int] = {}
        for row in stranded_rows:
            outcome = await _re_dispatch_capacity_blocked(str(row.id), str(row.organisation_id))
            redispatch_outcomes[outcome] = redispatch_outcomes.get(outcome, 0) + 1

        # FAR-143 follow-up — the sweep's raw terminal UPDATEs never run
        # finalize_cost, so the swept runs' journeys would never advance. Advance
        # each from its CREATE-STAMPED refs, fail-open per run (same pattern as
        # mark_complete / fail_run_terminal). Runs only re-dispatched (stranded
        # capacity-blocked) are NOT terminal — no advance. Each helper opens its
        # own session after the UPDATEs have committed, so it reads the run as
        # ``failed`` with ``completed_at`` set.
        for run_id, run_org_id in terminalised_run_ids:
            await _advance_journeys_from_stored_refs(async_engine, str(run_id), str(run_org_id), "failed")
            # FAR-162 (P6') — compensating daily fact for the same terminal
            # runs (separate RLS-scoped session, fail-open per run — one run's
            # facts failure must not fail the whole sweep).
            try:
                await _record_fact_for_terminal_failed_run(async_engine, str(run_id), str(run_org_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("pipeline_execution.sweep_terminal_facts_failed run=%s", run_id, exc_info=True)

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
    _stale_seconds: int,
    _claim_cap: int | None = None,
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
            # LIVE-BUG FIX (C3): same RLS org-context requirement as
            # ``claim_run_async`` — without it the resume claim UPDATE matches
            # ZERO rows under a NOBYPASSRLS role and the claim returns None.
            await c.execute(
                text(_SQL_SET_ORG_ID),
                {"val": org_id},
            )
            result = await c.execute(
                build_resume_claim_update(_stale_seconds=stale_seconds, _claim_cap=cap, claim_token=claim_token),
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
    claim_token: str | None = None,
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

    The ``claim_token`` kwarg is the stale token stamped into this job's kwargs
    by a previous attempt (PR #1003). SAQ retries re-invoke this function with
    ``**job.kwargs``, so the kwarg is accepted and intentionally IGNORED here —
    it is NOT passed into :func:`claim_resume_run_async`, which generates its
    own fresh token and re-stamps it via the job hash.
    """
    rid = uuid.UUID(run_id)
    oid = uuid.UUID(org_id)

    cap = _resolve_claim_cap(claim_cap)
    claim_token = await claim_resume_run_async(async_engine, str(rid), str(oid), claim_cap=cap)
    if not claim_token:
        _log.warning("resume_run: run %s not claimed (wrong state or claim cap)", rid)
        return {"status": "not_claimed"}

    # Stamp the claim token into the job hash so the after_process task_failure
    # hook can fence its terminal write (dist/runtime-core A1).
    if job is not None:
        try:
            job_kwargs = getattr(job, "kwargs", None)
            merged_kwargs = {**(job_kwargs if isinstance(job_kwargs, dict) else {}), "claim_token": claim_token}
            await job.update(kwargs=merged_kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("resume_run: job kwargs stamp failed for run %s", rid)

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

    outcome = await run_executor_with_watchdog(
        async_engine,
        run_id=str(rid),
        org_id=str(oid),
        executor=executor,
        job=job,
        claim_token=claim_token,
        execute_fn=lambda: executor.resume(
            run_id=rid,
            org_id=oid,
            resume_data=resume_data or {},
            claim_token=claim_token,
        ),
    )
    if outcome.get("status") == "complete":
        await mark_complete(async_engine, str(rid), str(oid), claim_token=claim_token)
    return outcome
