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
import threading
import uuid
from typing import Any

from saq import Status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from modulo.version import get_version

_log = logging.getLogger(__name__)

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


async def _mark_run_failed(run_id: str, org_id: str, claim_token: str | None = None) -> None:
    """Mark a run failed task_failure — guarded NOT IN terminal states (F3d).

    FENCED by *claim_token* (dist/runtime-core A1): when the failed job stamped
    its claim token (saq_worker.execute_run / pipeline_execution.resume_run),
    the write also requires ``claim_token = :tok`` so a failed job cannot
    task_failure a run that a successor already re-claimed. CANCEL-WINS:
    ``cancellation_requested = false``.
    """
    clauses = [
        "UPDATE runs SET status='failed', error_code='task_failure', completed_at=now() ",
        "WHERE id=:rid AND organisation_id=:oid ",
        "AND status NOT IN ('complete', 'cancelled', 'failed') ",
        "AND cancellation_requested = false ",
    ]
    params: dict[str, Any] = {"rid": run_id, "oid": org_id}
    if claim_token is not None:
        clauses.append("AND claim_token = CAST(:tok AS text)")
        params["tok"] = claim_token

    async with _open_factory()() as session, session.begin():
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, uuid.UUID(org_id))
        await session.execute(
            text("".join(clauses)),
            params,
        )


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
            await _mark_run_failed(outcome["run_id"], outcome["org_id"], claim_token=kwargs.get("claim_token"))
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
