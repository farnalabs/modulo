"""Run-admission healing primitives (FAR-604).

The 2026-09-04 PR Reviewer admission wedge (66 queued, zero starts for 13+h,
``capacity.pipeline``) had two structural causes this module fixes:

1. **Leaked slots** — a run left ``running`` by a crashed worker holds a
   pipeline slot forever (the legacy ``worker_lost`` sweep is scoped to
   non-SAQ rows with 5+ claims, so SAQ-dispatched leaked slots were never
   released). :func:`reconcile_pipeline_slots` is the periodic reconciliation
   sweep: it force-releases stale ``running`` slots and terminalises the run.
2. **Unbounded queue ratchet** — cron re-dispatches piled new pending runs
   onto a pipeline whose admission was wedged. :func:`evaluate_backpressure`
   lets trigger dispatch paths skip run creation when the pipeline's pending
   queue is over depth or age limits, and :func:`coalesce_pending_run`
   (db.crud.run) folds repeat webhook deliveries for the same work item into
   the already-pending run instead of minting new rows.

All three mechanisms are independent: the sweep never touches pending rows,
coalescing only folds UNSTARTED pending runs, and backpressure only refuses
NEW rows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# RLS set_config helper — same contract as pipeline_execution's sweep: the org
# enumeration runs in system context (organisations is the root table), then
# every per-org statement runs inside set_config('app.organisation_id', ...).
_SQL_SET_ORG_ID = "SELECT set_config('app.organisation_id', :val, true)"

# worker_lost is the house error code for worker death (error_codes.py maps it
# to the harness-dispatch failure class). The synthetic error_detail is safe:
# the daily-watcher hang-death detector keys on error_code == 'node_cancelled'
# ONLY (FAR-164), so a string detail here can never be miscounted as a hang.
_SLOT_RELEASE_DETAIL = "Slot reconciliation: heartbeat stale past threshold; pipeline slot force-released (FAR-604)."


async def _advance_released_run(async_engine: AsyncEngine, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Advance journeys + daily facts for a slot-released run (fail-open).

    Thin delegate to the shared langgraph-free orchestration
    (``run_terminal_advance.advance_terminalised_run`` — FAR-604 F4), the same
    sequence the legacy stale-run sweep uses — the duplication previously
    lived here line-for-line. run_admission must not import pipeline_execution:
    cron_helpers (reached from modulo.api.routes.health) would then
    transitively import langgraph and break the import-linter API-layer
    contract. The shared module's facts half runs UNCONDITIONALLY (no
    ``work_item_refs`` early return — a refs-less released run still gets its
    analytics fact; F3).
    """
    from modulo.core.run_terminal_advance import advance_terminalised_run

    await advance_terminalised_run(async_engine, run_id, org_id)


class SlotReconciliationError(RuntimeError):
    """Slot-reconciliation sweep failed partway; carries the partial counts.

    The SAQ cron wrapper (``saq_worker.slot_reconciliation``) catches this,
    persists the failure + partial ``released``/``per_pipeline`` counts to the
    Redis liveness key (so a silently dead sweep is visible to
    /healthz/ready), and re-raises so SAQ's ``retries=2`` engages. A sweep
    that swallows its own failures (the pre-fix error-dict return) re-opens
    the FAR-604 wedge invisibly.
    """

    def __init__(self, message: str, *, released: int, per_pipeline: dict[str, int]) -> None:
        super().__init__(message)
        self.released = released
        self.per_pipeline = per_pipeline


async def reconcile_pipeline_slots(
    async_engine: AsyncEngine,
    *,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    """Sweep stale ``running`` runs and force-release their pipeline slots.

    A run whose heartbeat (falling back to started_at/created_at) is older
    than *stale_seconds* (settings ``SLOT_RECONCILE_STALE_SECONDS``, default
    30 min) cannot be alive: the executor heartbeats every
    ``RUN_HEARTBEAT_SECONDS=30`` regardless of node progress. Such a run holds
    a ``max_concurrent_runs`` slot that admission can never reclaim — the
    leaked-slot half of the FAR-604 wedge.

    Per org (RLS-scoped), stale ``running`` rows are terminalised with the
    house worker-death code ``worker_lost``; each release is logged with the
    pipeline id, and journeys + daily facts are advanced post-commit exactly
    like the legacy stale-run sweep's terminalised rows (fail-open per run).
    ``awaiting_human`` runs are deliberately NOT swept (a human decision may
    legitimately take days) and fresh-heartbeat runs are never swept.

    Failure contract (FAR-604 F6): the sweep RAISES
    :class:`SlotReconciliationError` on failure (so the SAQ cron's
    ``retries=2`` engages) — it never swallows into a returned error dict.
    The post-release advance runs in its own ``finally``-equivalent block
    INDEPENDENT of the org-loop failure: rows already released still get
    their journeys + facts, and the raised error carries the PARTIAL
    ``released``/``per_pipeline`` counts achieved before the failure.

    Returns ``{"released": int, "per_pipeline": {pipeline_id: count}}``.
    """
    settings = get_settings()
    window = stale_seconds if stale_seconds is not None else settings.slot_reconcile_stale_seconds
    released: list[Any] = []
    per_pipeline: Counter[str] = Counter()
    sweep_error: BaseException | None = None
    try:
        async with async_engine.connect() as conn, conn.begin():
            org_result = await conn.execute(text("SELECT id FROM organisations"))
            org_ids: list[uuid.UUID] = [row[0] for row in org_result.all()]

        for org_id in org_ids:
            async with async_engine.connect() as conn, conn.begin():
                await conn.execute(text(_SQL_SET_ORG_ID), {"val": str(org_id)})
                result = await conn.execute(
                    text(
                        "UPDATE runs "
                        "SET status = 'failed', error_code = 'worker_lost', "
                        "error_detail = :detail, completed_at = now() "
                        "WHERE status = 'running' "
                        "AND organisation_id = :oid "
                        "AND cancellation_requested = false "
                        "AND COALESCE(heartbeat_at, started_at, created_at) "
                        "    < now() - (:stale_seconds * interval '1 second') "
                        "RETURNING id, organisation_id, pipeline_id"
                    ),
                    {"oid": str(org_id), "stale_seconds": window, "detail": _SLOT_RELEASE_DETAIL},
                )
                released.extend(result.all())
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        sweep_error = exc
        _log.exception("slot_reconciliation.sweep_failed")
    finally:
        # Post-release advance — independent of the org-loop failure (F6):
        # rows already released must still get their journeys + daily facts
        # even when a later org's release loop blew up.
        for row in released:
            per_pipeline[str(row.pipeline_id)] += 1
            _log.info(
                "slot_reconciliation.released run=%s pipeline=%s org=%s (heartbeat stale past %ss)",
                row.id,
                row.pipeline_id,
                row.organisation_id,
                window,
            )
            await _advance_released_run(async_engine, row.id, row.organisation_id)
        if released:
            _log.info(
                "slot_reconciliation.swept released=%d pipelines=%d",
                len(released),
                len(per_pipeline),
            )

    if sweep_error is not None:
        raise SlotReconciliationError(
            "slot reconciliation sweep failed",
            released=len(released),
            per_pipeline=dict(per_pipeline),
        ) from sweep_error
    return {"released": len(released), "per_pipeline": dict(per_pipeline)}


# ---------------------------------------------------------------------------
# D2 — webhook coalesce-key derivation (latest-wins queue coalescing)
# ---------------------------------------------------------------------------


def derive_webhook_coalesce_key(raw_payload: dict[str, Any] | None) -> str | None:
    """Derive a stable per-work-item coalesce key from a webhook payload.

    Derivation (documented contract — keep in sync with docs/architecture.md):

    * GitHub: ``repository.full_name`` plus a stable PR/event identifier —
      ``pull_request.number`` for PR events, falling back to
      ``issue.number`` for issue events. Key shape
      ``github:<full_name>:pr:<n>`` / ``github:<full_name>:issue:<n>``.
    * Anything else: ``None`` — no coalescing (the delivery creates a run).

    The key must be STABLE across re-deliveries of the same work item (a PR
    synchronize push keeps its number) while differing between work items, so
    Housekeeper's 15-minute re-dispatch churn coalesces onto one pending run
    instead of ratcheting the queue.
    """
    if not isinstance(raw_payload, dict):
        return None
    repository = raw_payload.get("repository")
    if not isinstance(repository, dict):
        return None
    repo = repository.get("full_name")
    if not isinstance(repo, str) or not repo:
        return None
    pull_request = raw_payload.get("pull_request")
    if isinstance(pull_request, dict):
        number = pull_request.get("number")
        if isinstance(number, int):
            return f"github:{repo}:pr:{number}"
        return None
    issue = raw_payload.get("issue")
    if isinstance(issue, dict):
        number = issue.get("number")
        if isinstance(number, int):
            return f"github:{repo}:issue:{number}"
    return None


def coalesce_enabled(trigger_config: dict[str, Any] | None) -> bool:
    """Read the per-trigger coalescing flag (default ON for webhook triggers).

    ``config_json.coalesce_pending`` — set ``false`` to always insert a new
    run per delivery. Any non-False value (including absent) means enabled.
    """
    if not trigger_config:
        return True
    return trigger_config.get("coalesce_pending") is not False


# ---------------------------------------------------------------------------
# D3 — dispatcher backpressure gate
# ---------------------------------------------------------------------------


async def evaluate_backpressure(
    session: Any,
    *,
    pipeline_id: uuid.UUID,
    oldest_age_seconds: int | None = None,
) -> tuple[bool, str]:
    """Decide whether a NEW trigger run for *pipeline_id* must be refused.

    Backpressure (FAR-604 D3): skip run creation when EITHER

    * the pending-queue depth (unstarted ``pending`` runs) exceeds
      ``max(3 x max_concurrent_runs, 5)`` — a queue 3x over the pipeline's
      admission rate is never going to drain; or
    * the OLDEST pending run's age exceeds *oldest_age_seconds* (settings
      ``TRIGGER_BACKPRESSURE_MAX_AGE_SECONDS``, default 60 min) — a wedged
      admission gate must stop accumulating deliveries even below the depth
      cap.

    Returns ``(skip, reason)`` — ``reason`` is a short stable token
    (``queue_depth`` / ``oldest_age``) for logs + TriggerEvent error_detail.
    Fail-open: a missing pipeline or a read error ADMITS the run (logged) —
    backpressure is an overload guard, never an admission authority.
    """
    from modulo.db.crud.run import get_pipeline_queue_depth

    if oldest_age_seconds is None:
        oldest_age_seconds = get_settings().trigger_backpressure_max_age_seconds
    try:
        max_concurrent = (
            await session.execute(
                text("SELECT max_concurrent_runs FROM pipelines WHERE id = :pid"),
                {"pid": str(pipeline_id)},
            )
        ).scalar_one_or_none()
        if max_concurrent is None:
            _log.warning("backpressure.pipeline_missing pipeline=%s (admitting)", pipeline_id)
            return False, ""
        depth, oldest_created_at = await get_pipeline_queue_depth(session, pipeline_id)
        depth_limit = max(3 * int(max_concurrent), 5)
        if depth > depth_limit:
            _log.info(
                "backpressure.queue_depth pipeline=%s depth=%d limit=%d — refusing new runs",
                pipeline_id,
                depth,
                depth_limit,
            )
            return True, f"queue_depth={depth} limit={depth_limit}"
        if oldest_created_at is not None:
            age = (datetime.now(UTC) - oldest_created_at).total_seconds()
            if age > oldest_age_seconds:
                _log.info(
                    "backpressure.oldest_age pipeline=%s oldest_age_s=%.0f limit=%d — refusing new runs",
                    pipeline_id,
                    age,
                    oldest_age_seconds,
                )
                return True, f"oldest_age={int(age)}s limit={oldest_age_seconds}s"
    except Exception:
        _log.warning("backpressure.check_failed pipeline=%s (admitting)", pipeline_id, exc_info=True)
        return False, ""
    return False, ""
