"""SAQ after_process hook — job-outcome reconciliation (plan F3d).

The hook extracts ``(function, status, error, kwargs)`` from the SAQ context and
delegates to the PURE :func:`_reconcile_job_outcome` classifier, then executes
the resulting action:

* ``execute_run``/``resume_run`` FAILED (exception, retries exhausted): mark the
  run ``failed`` with ``error_code='task_failure'``, guarded
  ``NOT IN ('complete', 'cancelled', 'failed')`` (never clobber a terminal row).
* ``fire_*``/report/cron FAILED: log + ingest an ``error_event`` (source='saq',
  function name); no run state.
* NO-OP SET: swept/cancelled/transient AND QUEUED/ACTIVE — the timeout/SIGTERM
  retry path leaves ``job.status=QUEUED`` at after_process time, so without this
  the shim would ingest a spurious outcome for a legitimately-retried job.
* Safe if the DB is down: log + leave for ``dispatcher_reconcile``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid
from typing import Any

from saq import Status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.version import get_version

_log = logging.getLogger(__name__)

# Internal error_code marker grammar for ``_mark_run_failed`` (the primitive's
# error_code is a code-owned constant — validated before it is inlined into
# the raw SQL so no caller can smuggle SQL through it).
_MARKER_ERROR_CODE_RE = re.compile(r"[a-z_]{1,64}")

# ``_mark_run_failed``'s SQL template. __ERROR_CODE__ / __TERMINAL_LIST__ /
# __CLAIM_FENCE__ are substituted with code-owned constants only (see the
# function body); :rid/:oid/:detail/:tok stay bound parameters.
_MARK_RUN_FAILED_SQL_TEMPLATE = (
    "UPDATE runs SET status='failed', error_code='__ERROR_CODE__', "
    "error_detail=:detail, completed_at=now() "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND status NOT IN (__TERMINAL_LIST__) "
    "AND status <> 'unknown' "
    "AND cancellation_requested = false "
    "__CLAIM_FENCE__"
)

# ``SET LOCAL lock_timeout`` (FAR-583): utility commands do not accept bind
# parameters (asyncpg rewrites :lt to $1 → "syntax error at or near $1"), so
# the value is inlined via the same __PLACEHOLDER__ + str.replace pattern as
# the template above. int() validation precedes stringification — only digits
# can reach the statement, never caller data.
_SET_LOCK_TIMEOUT_SQL_TEMPLATE = "SET LOCAL lock_timeout = __LOCK_TIMEOUT_MS__"

_ENGINE: AsyncEngine | None = None
_ENGINE_LOCK = threading.Lock()

# Terminal-but-success status needs no action; everything transient/retried
# needs no action either. ONLY a genuine FAILED (retries exhausted) classifies.
_NOOP_STATUSES = frozenset(
    {
        Status.NEW,
        Status.QUEUED,
        Status.ACTIVE,
        Status.ABORTING,
        Status.ABORTED,
        Status.COMPLETE,
    }
)

_SYSTEM_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                from modulo.settings import get_settings

                settings = get_settings()
                kw: dict[str, Any] = {"url": settings.database_url}
                if settings.modulo_db.lower() == "postgres":
                    kw["connect_args"] = {"timeout": 10, "ssl": False, "statement_cache_size": 0}
                    kw["pool_pre_ping"] = True
                    kw["pool_recycle"] = 3600
                    kw["pool_timeout"] = 30
                _ENGINE = create_async_engine(**kw)
    return _ENGINE


def _open_factory() -> async_sessionmaker[Any]:
    return async_sessionmaker(_get_engine(), expire_on_commit=False, autobegin=False)


# ---------------------------------------------------------------------------
# Pure classifier — no DB, no Redis, fully unit-testable
# ---------------------------------------------------------------------------


def _classify(function: str, status: Any, error: str | None, kwargs: dict[str, Any] | None) -> dict[str, Any]:
    """Pure: classify a finished SAQ job into an action.

    Returns one of:

      * ``{"action": "noop"}`` — successful, transient, swept, cancelled, or a
        legitimately-retried (QUEUED/ACTIVE) job.
      * ``{"action": "fail_run", "run_id", "org_id"}`` — execute_run/resume_run
        genuinely failed (retries exhausted).
      * ``{"action": "ingest_error", "function", "org_id", "message"}`` —
        fire_*/report/cron genuinely failed; ingest with source='saq'.
    """
    if status in _NOOP_STATUSES or status is None:
        return {"action": "noop"}

    if status != Status.FAILED:
        return {"action": "noop"}

    kwargs = kwargs or {}
    run_id = kwargs.get("run_id")
    org_id = kwargs.get("org_id")

    is_run_job = function.endswith(("execute_run", "resume_run"))
    if is_run_job and run_id:
        return {
            "action": "fail_run",
            "run_id": str(run_id),
            "org_id": str(org_id) if org_id else str(_SYSTEM_ORG_ID),
            "error": error,
        }

    # fire_*/report/cron failure — log + ingest error event, no run state.
    return {
        "action": "ingest_error",
        "function": function,
        "org_id": str(org_id) if org_id else str(_SYSTEM_ORG_ID),
        "message": f"SAQ job {function} failed: {error or 'unknown error'}",
        "error": error,
    }


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------


async def _mark_run_failed(
    run_id: str,
    org_id: str,
    claim_token: str | None = None,
    error_detail: str | None = None,
    *,
    error_code: str = "task_failure",
    lock_timeout_ms: int | None = None,
) -> int:
    """Mark a run failed — guarded NOT IN any terminal state + not 'unknown'.

    FENCED by *claim_token* (dist/runtime-core A1): when the failed job stamped
    its claim token (saq_worker.execute_run / pipeline_execution.resume_run),
    the write also requires ``claim_token = :tok`` so a failed job cannot
    task_failure a run that a successor already re-claimed. CANCEL-WINS:
    ``cancellation_requested = false``.

    The status guard is the FULL ``TERMINAL_STATUSES`` frozenset
    (db/models/run.py — the 9-state terminal vocabulary) plus an EXPLICIT
    ``'unknown'`` exclusion (FAR-583): an ``unknown``-status run (FAR-410,
    non-terminal recovery state) is NEVER transitioned by this primitive —
    callers that must surface a failure for an unknown run emit the event
    only (rowcount 0) and leave the row for operator reconciliation.

    *error_code* is the failure marker written to ``runs.error_code``
    (default ``task_failure``; the FAR-583 dual-write orchestration passes
    ``dual_write_failed``). It is validated against the internal marker
    grammar before being inlined — it is a code-owned constant, never user
    input.

    *error_detail* is sanitized (secret-pattern redaction) BEFORE the UPDATE —
    ``runs.error_detail`` is ``Text`` (widened from ``String(5000)`` by
    migration 0199), so untruncated details no longer raise DataError; the
    generic except in
    :func:`after_process` would swallow and the run would NEVER be marked
    failed (the exact failure this fix exists to prevent). ``None`` is written
    when the detail is falsy — never ``""`` (an empty-string detail flips the
    daily-watcher detail_available flag; NULL does not).

    *lock_timeout_ms* (FAR-583): when set (Postgres only), a
    ``SET LOCAL lock_timeout`` precedes the UPDATE so a lock collision with a
    concurrent writer degrades to a bounded query-cancellation (rowcount 0)
    instead of hanging the abort path. The caller opens its OWN session via
    ``_open_factory``; the SET LOCAL is transaction-local and rolls back with
    it.

    FAR-224 decision — SWEEP-ONLY, not inline: this raw-SQL write deliberately
    does NOT call the classification hook inline (unlike the fenced
    ``crud.run`` terminal write). The 60s ``dispatcher_reconcile`` →
    ``run_classification_reconcile`` sweep backfills the record within one
    tick: it matches every ``status='failed'`` row with
    ``run_classification IS NULL`` (this UPDATE sets both ``status='failed'``
    and ``completed_at=now()``), and the FAR-190 streak walk runs AFTER the
    classification reconcile in the SAME tick, so a run failed here is counted
    in the same sweep cycle. The streak engine reads classification only at
    sweep time, the FAR-191 streak-status reader degrades gracefully on NULL,
    and no terminalization-time synchronous consumer exists — so the bounded
    60s lag is acceptable. Re-inline (following the crud/run.py contract) only
    if a synchronous terminalization-time consumer appears.

    Returns the number of rows updated — 0 means the guards rejected the write
    (superseded / already terminal / cancellation requested / lock timeout).
    """
    from modulo.db.models.run import TERMINAL_STATUSES

    if not _MARKER_ERROR_CODE_RE.fullmatch(error_code):
        raise ValueError(f"invalid error_code marker: {error_code!r}")
    # Dynamic SQL via the __PLACEHOLDER__ + str.replace pattern (the
    # trigger_streak precedent): bandit/ruff S608 flag f-stringed text(), and
    # the only interpolated values here are code-owned constants (the
    # validated error_code marker + the fixed TERMINAL_STATUSES vocabulary) —
    # never caller data. Bind params (:rid/:oid/:detail/:tok) stay bound.
    terminal_list = ", ".join("'" + status + "'" for status in sorted(TERMINAL_STATUSES))
    statement = _MARK_RUN_FAILED_SQL_TEMPLATE.replace("__ERROR_CODE__", error_code)
    statement = statement.replace("__TERMINAL_LIST__", terminal_list)
    params: dict[str, Any] = {"rid": run_id, "oid": org_id}
    if claim_token is not None:
        statement = statement.replace("__CLAIM_FENCE__", "AND claim_token = CAST(:tok AS text) ")
        params["tok"] = claim_token
    else:
        statement = statement.replace("__CLAIM_FENCE__", "")

    from modulo.core.pipeline_engine.error_codes import sanitize_error_text

    if error_detail:
        params["detail"] = sanitize_error_text(error_detail, limit=None)
    else:
        params["detail"] = None

    async with _open_factory()() as session, session.begin():
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, uuid.UUID(org_id))
        if lock_timeout_ms is not None:
            bind = session.get_bind()
            if asyncio.iscoroutine(bind):
                bind = await bind
            if bind.dialect.name == "postgresql":
                # Utility commands do not accept bind parameters (asyncpg
                # rewrites :lt to $1 → "syntax error at or near $1"), so the
                # validated integer is inlined via the __PLACEHOLDER__ +
                # str.replace pattern (see _SET_LOCK_TIMEOUT_SQL_TEMPLATE) —
                # an f-stringed text() violates the raw-SQL architecture rule.
                lock_timeout_statement = _SET_LOCK_TIMEOUT_SQL_TEMPLATE.replace(
                    "__LOCK_TIMEOUT_MS__", str(int(lock_timeout_ms))
                )
                await session.execute(text(lock_timeout_statement))
        result = await session.execute(
            text(statement),
            params,
        )
        return int(result.rowcount)


async def _record_task_failure_facts(run_id: str, org_id: str) -> None:
    """Record the compensating analytics fact for a task_failure run (P6').

    Runs in a SEPARATE session from the mark (which is already committed), so
    a failure inside the facts write — including ``CancelledError`` — can
    NEVER roll back the committed ``failed`` transition. Re-selects the Run
    ORM entity AFTER the mark (a pre-update entity would record
    ``status='running'`` with a NULL ``completed_at``). None-guarded and
    fail-open via the shared :func:`record_fact_for_terminal_failed_run`
    helper (the same one the sweep / dispatcher terminalizers use — the
    split that reviewers flagged as the weakest design point of #1166 is gone).
    """
    try:
        async with _open_factory()() as session, session.begin():
            from modulo.db.rls import set_rls_org

            await set_rls_org(session, uuid.UUID(org_id))

            from modulo.core.analytics import record_fact_for_terminal_failed_run
            from modulo.db.crud.run import get_run

            run = await get_run(session, uuid.UUID(run_id))
            await record_fact_for_terminal_failed_run(session, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("saq_hooks.task_failure_facts_failed run=%s", run_id)


async def _ingest_error_event(
    function: str,
    org_id: str,
    message: str,
    error: str | None,
) -> None:
    parsed = uuid.UUID(org_id)
    if parsed == _SYSTEM_ORG_ID:
        _log.error(
            "SAQ system error (no tenant context) — skipping DB ingest: function=%s message=%s error=%s",
            function,
            message,
            error,
        )
        return

    from modulo.core.error_tracking import ErrorIngestionService
    from modulo.db.rls import set_rls_org

    async with _open_factory()() as session, session.begin():
        await set_rls_org(session, parsed)
        await ErrorIngestionService().ingest(
            session,
            parsed,
            {
                "level": "error",
                "message": message,
                "source": "saq",
                "stacktrace": error,
                "context_json": {"function": function},
                "environment": os.environ.get("MODULO_ENV", "development"),
                "version": get_version(),
            },
        )


async def after_process(ctx: dict[str, Any]) -> None:
    """SAQ after_process hook — extract outcome and reconcile (plan F3d)."""
    job = ctx.get("job")
    if job is None:
        return

    function = getattr(job, "function", "")
    status = getattr(job, "status", None)
    error = getattr(job, "error", None)
    kwargs = getattr(job, "kwargs", None) or {}

    outcome = _classify(str(function), status, error, kwargs)
    action = outcome["action"]
    if action == "noop":
        return

    try:
        if action == "fail_run":
            _log.error(
                "SAQ run job %s failed for run %s (task_failure)",
                function,
                outcome["run_id"],
            )
            rowcount = await _mark_run_failed(
                outcome["run_id"],
                outcome["org_id"],
                claim_token=kwargs.get("claim_token"),
                error_detail=outcome.get("error"),
            )
            if rowcount == 0:
                # Superseded / guard-rejected: the run was already terminal or
                # re-claimed by a successor — nothing to compensate.
                _log.warning(
                    "saq_hooks.task_failure_superseded run=%s (rowcount 0)",
                    outcome["run_id"],
                )
            else:
                # rowcount == 1: the run is now failed — write the compensating
                # analytics fact (separate session, fail-open).
                await _record_task_failure_facts(outcome["run_id"], outcome["org_id"])
        elif action == "ingest_error":
            _log.error("SAQ job %s failed: %s", function, error)
            await _ingest_error_event(
                outcome["function"],
                outcome["org_id"],
                outcome["message"],
                outcome.get("error"),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Safe if DB is down: log + leave for dispatcher_reconcile.
        _log.exception("saq_hooks.after_process_reconcile_failed function=%s", function)
