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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.models.run import AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS
from modulo.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, sqlstate_of
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

# RLS set_config helper — same contract as pipeline_execution's sweep: the org
# enumeration runs in system context (organisations is the root table), then
# every per-org statement runs inside set_config('app.organisation_id', ...).
_SQL_SET_ORG_ID = "SELECT set_config('app.organisation_id', :val, true)"

# heartbeat_stale is the FAR-604 P1 code for the heartbeat-stale slot
# force-release: DISTINCT from worker_lost (which maps to
# harness.dispatch_failed — "never dispatched") so analytics can tell a run
# that never started from one that was dispatched and then went silent. The
# raw spelling is canonicalized via LEGACY_ALIASES in error_codes.py to the
# registered ``harness.heartbeat_stale`` entry (never harness.unknown). The
# synthetic error_detail is safe: the daily-watcher hang-death detector keys
# on error_code == 'node_cancelled' ONLY (FAR-164), so a string detail here
# can never be miscounted as a hang.
_SLOT_RELEASE_DETAIL = "Slot reconciliation: heartbeat stale past threshold; pipeline slot force-released (FAR-604)."

# FAR-779 / FAR-812: heartbeat-stale auto-retry budget for this sweep lives in
# settings as HEARTBEAT_STALE_RETRY_BUDGET (default 3 since FAR-812, was 1).
# A ``running`` run swept as heartbeat-stale is RESET to ``pending`` for
# re-dispatch (clearing ``dispatched_at``/``dispatcher``/``heartbeat_at``) so
# ``dispatcher_reconcile`` re-dispatches it on its next 60s tick — while its
# claim_count is within budget. The budget is checked against
# ``runs.claim_count``: each SAQ dequeue+claim increments it, so a run claimed
# N times that still goes heartbeat-stale is genuinely stuck and is
# terminal-failed once claim_count EXCEEDS the budget. A zero-node run is
# always safe to re-dispatch (nothing can double-execute); the budget absorbs a
# transient dispatch wobble so a task that never started is not lost.


def _is_row_lock_timeout(exc: BaseException) -> bool:
    """True when *exc* is the bounded ``lock_timeout`` expiry (SQLSTATE 55P03).

    FAR-1592: both periodic sweeps in this module write the hot ``runs`` table
    under the transaction-scoped ``db.crud.row_lock.
    set_mutation_row_lock_timeout`` bound (``Settings.
    mutation_row_lock_timeout_ms``), so a contended row lock can wait at most
    that long — never silently past the Fly HAProxy 30-minute session window
    (the unbounded wait that got prod connections culled mid-operation;
    FAR-1524 O11 / FAR-1584).

    :func:`modulo.db.sqlstates.sqlstate_of` walks the whole chain
    (``.orig``/``__cause__``/``__context__``, incl. savepoint-rollback
    wrappers), so both the raw-driver and SQLAlchemy-wrapped shapes are
    recognised. Same predicate FAR-1584's ``core.dispatch.
    _is_row_lock_timeout`` uses. Any OTHER failure is not a lock timeout and
    keeps propagating to the sweep's failure contract.
    """
    return sqlstate_of(exc) == LOCK_NOT_AVAILABLE_SQLSTATE


async def _advance_released_run(async_engine: AsyncEngine, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Advance journeys + daily facts + audit for a slot-released run (fail-open).

    Thin delegate to the shared langgraph-free orchestration
    (``run_terminal_advance.advance_terminalised_run`` — FAR-604 F4), the same
    sequence the legacy stale-run sweep uses — the duplication previously
    lived here line-for-line. run_admission must not import pipeline_execution:
    cron_helpers (reached from modulo.api.routes.health) would then
    transitively import langgraph and break the import-linter API-layer
    contract. The shared module's facts half runs UNCONDITIONALLY (no
    ``work_item_refs`` early return — a refs-less released run still gets its
    analytics fact; F3), and its audit half records the terminalisation on the
    org chain with this sweep as the ``actor_source`` (FAR-1549).
    """
    from modulo.core.run_terminal_advance import advance_terminalised_run

    await advance_terminalised_run(async_engine, run_id, org_id, source="slot_reconciliation")


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


class HitlParkError(RuntimeError):
    """HITL park sweep failed partway; carries the partial ``parked`` count.

    The SAQ cron wrapper (``saq_worker.hitl_park_sweep``) persists the failure
    + partial ``parked`` count to the shared Redis liveness key (so a silently
    dead sweep is visible to /healthz/ready, mirroring the slot-reconciliation
    contract) and re-raises so SAQ's ``retries=2`` engages. A sweep that
    swallows its own failures re-opens the visibility gap invisibly — parking
    is post-D1 hygiene (a parked run no longer consumes pipeline capacity),
    so without the liveness key a dead sweep would delay the "expired —
    parked" transition forever.
    """

    def __init__(self, message: str, *, parked: int) -> None:
        super().__init__(message)
        self.parked = parked


# Guarded park UPDATE (FAR-604 D2). Re-validated at execution time so it is
# atomic + idempotent:
#   * ``status = :parked_status`` target, source ``status = :awaiting_status``
#     — only a run actually waiting on a human is parked (never running/
#     claimed rows; a second tick finds no rows — the status-based idempotency,
#     qa F13).
#   * ``cancellation_requested = false`` — a cancellation-requested run is
#     being cancelled; the cancel path owns its status.
#   * EXISTS an expired UNCLAIMED undecided gate past the review deadline — the
#     incident predicate (gate open + unanswered + past its deadline).
#   * NOT EXISTS any undecided gate that is CLAIMED or still inside its window
#     — a run must only park when EVERY one of its open gates is
#     expired-unclaimed (multi-gate runs are never parked on a stale orphan).
# The gate row itself is NOT touched here (park ≠ decide — the sweep must
# never write ``decision``; see Linear FAR-609): it stays open and claimable.
# The status literals are bound params (qa F15) so every write site names the
# same constants.
#
# FAR-1257 review deadline: each claim carries EITHER an absolute
# ``terminalize_at`` stamped at fire time (pipeline override > org default >
# instance/env default) or NULL for legacy rows. Stamped rows park at
# ``terminalize_at + :park_margin_seconds``; unstamped rows keep the exact
# legacy ``expires_at + :grace_seconds`` arithmetic. The margin is derived in
# :func:`_park_margin_seconds` so the TERMINALIZING deadline (``terminalize_at``
# itself, collected by dispatcher_reconcile) always lands first.
_PARK_RUNS_SQL = text(
    "UPDATE runs SET status = :parked_status "
    "WHERE runs.organisation_id = :oid "
    "AND runs.status = :awaiting_status "
    "AND runs.cancellation_requested = false "
    "AND EXISTS ("
    "  SELECT 1 FROM hitl_claims hc "
    "  WHERE hc.organisation_id = runs.organisation_id "
    "  AND hc.run_id = runs.id "
    "  AND hc.decision IS NULL "
    "  AND hc.account_id IS NULL "
    "  AND ((hc.terminalize_at IS NOT NULL "
    "        AND (hc.terminalize_at + (:park_margin_seconds * interval '1 second')) < now()) "
    "       OR (hc.terminalize_at IS NULL "
    # Four closing parens: ``- (:grace * interval)``, the ``OR (`` arm, the
    # wrapping ``((`` two-arm group, and the ``EXISTS (`` itself — a missing
    # one is a Postgres syntax error at RETURNING (caught by the FAR-1257
    # integration test, invisible to substring unit assertions).
    "           AND hc.expires_at < now() - (:grace_seconds * interval '1 second')))) "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM hitl_claims hc2 "
    "  WHERE hc2.organisation_id = runs.organisation_id "
    "  AND hc2.run_id = runs.id "
    "  AND hc2.decision IS NULL "
    "  AND (hc2.account_id IS NOT NULL "
    "       OR (hc2.terminalize_at IS NOT NULL "
    "           AND (hc2.terminalize_at + (:park_margin_seconds * interval '1 second')) >= now()) "
    "       OR (hc2.terminalize_at IS NULL "
    # Four closing parens: ``- (:grace * interval)``, the ``OR (`` arm, the
    # wrapping ``AND (`` group, and the ``NOT EXISTS (`` itself.
    "           AND hc2.expires_at >= now() - (:grace_seconds * interval '1 second')))) "
    "RETURNING runs.id, runs.pipeline_id, runs.organisation_id"
)

#: FAR-1257: floor on the park margin — five ``dispatcher_reconcile`` ticks
#: (60s each). ``HITL_PARK_GRACE_SECONDS`` may be configured down to its own
#: ``ge=60`` floor, which would otherwise let the park sweep match in the same
#: tick as (or one tick before) the terminalizing sweep that must win. The
#: floor makes "cancel precedes park" hold by construction even at that
#: extreme config, at the cost of parking 4 minutes later than configured.
_PARK_MARGIN_FLOOR_SECONDS = 300


def _park_margin_seconds(park_grace_seconds: int) -> int:
    """Margin between the review deadline (``terminalize_at``) and the park.

    Park is measured as one full park-grace window AFTER the review deadline,
    floored at :data:`_PARK_MARGIN_FLOOR_SECONDS`. Anchoring on the same
    deadline the terminalizer uses is what makes the ordering structural: the
    terminalizer collects at ``terminalize_at`` (any 60s reconcile tick
    thereafter), the park sweep can only match ``park_margin >= 300s`` later,
    so a run is cancelled — releasing its org slot — long before it could be
    parked.

    Under the shipped defaults this is observably identical to the legacy
    arithmetic: the run leaves ``awaiting_human`` via the cancel sweep at
    ``terminalize_at`` (= ``expires_at + 3600``) and is therefore no longer a
    park candidate at all (park requires source status ``awaiting_human``).
    """
    return max(int(park_grace_seconds), _PARK_MARGIN_FLOOR_SECONDS)


async def park_expired_hitl_runs(
    async_engine: AsyncEngine,
    *,
    grace_seconds: int | None = None,
) -> dict[str, Any]:
    """Park runs whose HITL review expired unanswered past the grace window (D2).

    The HITL-capacity half of the FAR-604 design: a run waiting at a gate the
    human never answered used to sit ``awaiting_human`` forever (the
    2026-09-04 incident: 20 awaiting_human runs held a 20-cap pipeline for
    26h). The D1 capacity exclusion already stops that count; this sweep is
    the VISIBILITY + lifecycle half — it moves each such run to the dedicated
    non-terminal ``hitl_parked`` status (still in ACTIVE_RUN_STATUSES, so
    every consumer that treats awaiting_human as in-flight treats a parked
    run identically) once ALL of the run's open gates are unclaimed AND past
    their review deadline. For a legacy claim (``terminalize_at`` NULL) that is
    still ``expires_at + grace`` (settings ``HITL_PARK_GRACE_SECONDS``,
    default 24h); for a FAR-1257-stamped claim it is ``terminalize_at`` plus
    the margin from :func:`_park_margin_seconds`, which keeps the TERMINALIZING
    sweep (dispatcher_reconcile, which collects at ``terminalize_at`` itself)
    strictly ahead of this one.

    PARK ≠ DECIDE: the gate row is never touched here — ``decision`` stays
    NULL and the gate stays OPEN AND CLAIMABLE (a claim on an expired gate
    takes a fresh TTL; the decision-time un-park in ``HITLManager._decide``
    re-enters the run into normal admission). The ``hitl_parked`` STATUS
    itself is the parked signal (qa F13 — the proposed ``hitl_claims
    .parked_at`` column was dropped: it duplicated the status, went stale on
    a re-park, and added schema + phantom-count surface).

    Per org (RLS-scoped), one guarded UPDATE parks and RETURNs the parked
    rows; every park is logged loudly (``hitl_park.parked``). Re-running is a
    no-op (a parked run no longer matches the source status — idempotency by
    the status predicate itself, qa F13). The org's count is accumulated only
    after its UPDATE succeeds (qa F6 — a failure can never log a phantom
    park: the failed org's transaction rolled back, so its rows are neither
    parked nor counted).
    Failure contract: raises :class:`HitlParkError` (with the partial
    ``parked`` count) so the SAQ cron's ``retries=2`` engages — never a
    swallowed error dict; the wrapper persists the outcome to the shared
    Redis liveness key first (F5).

    Lock bound (FAR-1592): each per-org transaction issues the
    transaction-scoped ``lock_timeout`` bound (``db.crud.row_lock.
    set_mutation_row_lock_timeout``, ``Settings.
    mutation_row_lock_timeout_ms``) BEFORE its first lock. A bounded wait
    that expires (SQLSTATE 55P03) is NOT a sweep failure: the org
    transaction rolled back whole, so no run left ``awaiting_human`` and the
    next 5-minute tick re-parks it — that org is SKIPPED with a WARNING
    (never silent, never a lost recovery) while the remaining orgs still
    run; every other failure keeps the ``HitlParkError`` contract above.

    Returns ``{"parked": int}``.
    """
    settings = get_settings()
    window = grace_seconds if grace_seconds is not None else settings.hitl_park_grace_seconds
    park_margin = _park_margin_seconds(window)
    parked: list[Any] = []
    sweep_error: BaseException | None = None
    try:
        async with async_engine.connect() as conn, conn.begin():
            org_result = await conn.execute(text("SELECT id FROM organisations"))
            org_ids: list[uuid.UUID] = [row[0] for row in org_result.all()]

        for org_id in org_ids:
            try:
                async with async_engine.connect() as conn, conn.begin():
                    from modulo.db.crud.row_lock import set_mutation_row_lock_timeout

                    # FAR-1592: same class as the slot sweep above — this
                    # multi-row ``UPDATE runs`` is the hot table, so the
                    # transaction-scoped bound (``Settings.
                    # mutation_row_lock_timeout_ms``) is issued BEFORE the
                    # first lock the org transaction takes.
                    await set_mutation_row_lock_timeout(conn)
                    await conn.execute(text(_SQL_SET_ORG_ID), {"val": str(org_id)})
                    result = await conn.execute(
                        _PARK_RUNS_SQL,
                        {
                            "oid": str(org_id),
                            "grace_seconds": window,
                            "park_margin_seconds": park_margin,
                            "parked_status": HITL_PARKED_STATUS,
                            "awaiting_status": AWAITING_HUMAN_STATUS,
                        },
                    )
                    rows = result.all()
                    if not rows:
                        continue
                    # qa F6: the count/log is recorded only after the park UPDATE
                    # succeeded — the single write of the org transaction, so a
                    # failure here rolls the park back BEFORE the rows are ever
                    # counted (no phantom "hitl_park.parked" events).
                    parked.extend(rows)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _is_row_lock_timeout(exc):
                    raise
                # FAR-1592: bounded-wait expiry (55P03). The org transaction
                # rolled back whole, so NONE of its runs moved out of
                # ``awaiting_human`` — every candidate row is still parked-
                # eligible and the next 5-minute tick re-parks it. A skip,
                # never a lost recovery; WARNING + full chain with the org and
                # window so it is observable, never a silent no-op. Other
                # SQLSTATEs re-raise into the HitlParkError contract above.
                _log.warning(
                    "hitl_park.org_lock_timeout org=%s grace_seconds=%d "
                    "park_margin_seconds=%d (SQLSTATE 55P03 from the bounded "
                    "mutation_row_lock_timeout_ms wait) — org transaction "
                    "rolled back with NO rows parked; the expired runs stay "
                    "awaiting_human and are re-parked by the next sweep tick",
                    org_id,
                    window,
                    park_margin,
                    exc_info=True,
                )
                continue
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        sweep_error = exc
        _log.exception("hitl_park.sweep_failed")
    finally:
        # Loud per-run event — the parked run left review state; operators
        # must see every park (the incident class this prevents is silent).
        for row in parked:
            _log.warning(
                "hitl_park.parked run=%s pipeline=%s org=%s (gate expired unanswered past %ss grace)",
                row.id,
                row.pipeline_id,
                row.organisation_id,
                window,
            )
        if parked:
            _log.warning("hitl_park.swept parked=%d", len(parked))

    if sweep_error is not None:
        # FAR-1549: record the parks that DID land before re-raising — a
        # partial sweep still changed run state, and the status guard below
        # drops any row whose transaction rolled back.
        await _record_parked_run_audits(async_engine, parked, window)
        raise HitlParkError("HITL park sweep failed", parked=len(parked)) from sweep_error
    await _record_parked_run_audits(async_engine, parked, window)
    return {"parked": len(parked)}


async def _record_parked_run_audits(async_engine: AsyncEngine, parked: list[Any], window: int) -> None:
    """Record each landed park on the org's audit chain (FAR-1549).

    The per-org park transactions above have already committed by the time
    this runs, so the append opens its OWN session (it can neither roll back
    nor be rolled back by the sweep) and writes with the SYSTEM actor — there
    is no request principal in scope for a cron sweep. Best-effort: a failure
    is logged and swallowed; the park itself is already committed and must not
    be turned into a sweep failure by its own audit record.
    """
    if not parked:
        return
    try:
        from modulo.core.audit_logger.background import record_run_state_change_audits

        await record_run_state_change_audits(
            async_sessionmaker(async_engine, expire_on_commit=False, autobegin=False),
            [(row.id, row.organisation_id) for row in parked],
            event_type="hitl.run_parked",
            expected_statuses={HITL_PARKED_STATUS},
            actor_source="hitl_park_sweep",
            log_key="run_admission.hitl_park_audit_failed",
            summary_prefix=f"Run parked unanswered past {window}s grace by",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("hitl_park.audit_failed parked=%d", len(parked))


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
    FAR-604 P1 code ``heartbeat_stale`` (canonicalized to
    ``harness.heartbeat_stale`` — distinct from the legacy ``worker_lost``
    "never dispatched" class); each release is logged with the pipeline id,
    and journeys + daily facts are advanced post-commit exactly
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

    Lock bound (FAR-1592): each per-org transaction issues the
    transaction-scoped ``lock_timeout`` bound (``db.crud.row_lock.
    set_mutation_row_lock_timeout``, ``Settings.
    mutation_row_lock_timeout_ms``) BEFORE its first lock, so the multi-row
    ``UPDATE runs`` never waits unbounded (FAR-1584's class, applied to this
    sweep). A bounded wait that expires (SQLSTATE 55P03) is NOT a sweep
    failure: the org transaction rolled back whole, so its stale ``running``
    rows are untouched and the next 5-minute tick re-processes them — the
    org is SKIPPED with a WARNING (never silent, never a lost recovery) and
    the remaining orgs still run. Any other failure keeps the F6 contract
    above.

    Returns ``{"released": int, "per_pipeline": {pipeline_id: count}}``.
    """
    settings = get_settings()
    window = stale_seconds if stale_seconds is not None else settings.slot_reconcile_stale_seconds
    retry_budget = settings.heartbeat_stale_retry_budget
    released: list[Any] = []
    retried: list[Any] = []
    per_pipeline: Counter[str] = Counter()
    sweep_error: BaseException | None = None
    try:
        async with async_engine.connect() as conn, conn.begin():
            org_result = await conn.execute(text("SELECT id FROM organisations"))
            org_ids: list[uuid.UUID] = [row[0] for row in org_result.all()]

        for org_id in org_ids:
            try:
                async with async_engine.connect() as conn, conn.begin():
                    from modulo.db.crud.row_lock import set_mutation_row_lock_timeout

                    # FAR-1592: bound THIS org transaction's row-lock waits
                    # first (set_config takes no lock, so it can never disturb
                    # lock ordering) — the multi-row UPDATE below is the hot
                    # ``runs`` table, so a contended lock waits at most
                    # Settings.mutation_row_lock_timeout_ms, never the
                    # unbounded, >=30-min silent wait HAProxy culls mid-
                    # operation (FAR-1524 O11 / FAR-1584).
                    await set_mutation_row_lock_timeout(conn)
                    await conn.execute(text(_SQL_SET_ORG_ID), {"val": str(org_id)})
                    # FAR-779 / FAR-812: auto-retry heartbeat-stale runs instead of
                    # terminal-failing them immediately.  When claim_count <=
                    # retry_budget (settings.heartbeat_stale_retry_budget), the run is
                    # reset to pending (clearing dispatched_at/dispatcher/
                    # heartbeat_at) so dispatcher_reconcile re-dispatches it on its
                    # next 60s tick. When claim_count > budget, the run is
                    # terminal-failed — it has been claimed multiple times and
                    # still goes heartbeat-stale, meaning it is genuinely stuck.
                    result = await conn.execute(
                        text(
                            "UPDATE runs SET "
                            "status = CASE "
                            "  WHEN claim_count <= :retry_budget THEN 'pending' "
                            "  ELSE 'failed' "
                            "END, "
                            "error_code = 'heartbeat_stale', "
                            "error_detail = :detail, "
                            "completed_at = CASE "
                            "  WHEN claim_count <= :retry_budget THEN NULL "
                            "  ELSE now() "
                            "END, "
                            "dispatched_at = NULL, "
                            "dispatcher = NULL, "
                            "heartbeat_at = NULL "
                            "WHERE status = 'running' "
                            "AND organisation_id = :oid "
                            "AND cancellation_requested = false "
                            "AND COALESCE(heartbeat_at, started_at, created_at) "
                            "    < now() - (:stale_seconds * interval '1 second') "
                            "RETURNING id, organisation_id, pipeline_id, claim_count"
                        ),
                        {
                            "oid": str(org_id),
                            "stale_seconds": window,
                            "detail": _SLOT_RELEASE_DETAIL,
                            "retry_budget": retry_budget,
                        },
                    )
                    for row in result.all():
                        if getattr(row, "claim_count", 0) <= retry_budget:
                            retried.append(row)
                        else:
                            released.append(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _is_row_lock_timeout(exc):
                    raise
                # FAR-1592: the bounded wait expired (55P03) — a live writer
                # (executor heartbeat / claim) holds a row this org's sweep
                # needs. The org transaction rolled back WHOLE, so NOT ONE of
                # its stale ``running`` rows moved: they are still ``running``
                # with a stale heartbeat and the next 5-minute sweep tick
                # re-processes them — a skip, never a lost recovery. WARNING
                # + full chain with the org and window: observable, never a
                # silent no-op. Other SQLSTATEs are not lock timeouts and
                # re-raise into the failure contract above.
                _log.warning(
                    "slot_reconciliation.org_lock_timeout org=%s stale_seconds=%d "
                    "retry_budget=%d (SQLSTATE 55P03 from the bounded "
                    "mutation_row_lock_timeout_ms wait) — org transaction rolled "
                    "back with NO rows swept; the heartbeat-stale runs stay "
                    "``running`` and are re-processed by the next sweep tick",
                    org_id,
                    window,
                    retry_budget,
                    exc_info=True,
                )
                continue
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        sweep_error = exc
        _log.exception("slot_reconciliation.sweep_failed")
    finally:
        # Post-release advance — only for terminal-failed runs (FAR-779):
        # retried runs (reset to pending) are NOT terminal and must NOT get
        # their journeys + daily facts advanced — they will be re-dispatched
        # by dispatcher_reconcile.
        for row in released:
            per_pipeline[str(row.pipeline_id)] += 1
            _log.info(
                "slot_reconciliation.released run=%s pipeline=%s org=%s "
                "(heartbeat stale past %ss, claim_count=%d exceeded budget %d)",
                row.id,
                row.pipeline_id,
                row.organisation_id,
                window,
                getattr(row, "claim_count", 0),
                retry_budget,
            )
            await _advance_released_run(async_engine, row.id, row.organisation_id)
        for row in retried:
            per_pipeline[str(row.pipeline_id)] += 1
            _log.warning(
                "slot_reconciliation.retried run=%s pipeline=%s org=%s "
                "(heartbeat stale past %ss, claim_count=%d <= budget %d"
                " — reset to pending for re-dispatch)",
                row.id,
                row.pipeline_id,
                row.organisation_id,
                window,
                getattr(row, "claim_count", 0),
                retry_budget,
            )
        total_swept = len(released) + len(retried)
        if total_swept:
            _log.info(
                "slot_reconciliation.swept released=%d retried=%d pipelines=%d",
                len(released),
                len(retried),
                len(per_pipeline),
            )

    if sweep_error is not None:
        raise SlotReconciliationError(
            "slot reconciliation sweep failed",
            released=len(released) + len(retried),
            per_pipeline=dict(per_pipeline),
        ) from sweep_error
    return {"released": len(released), "retried": len(retried), "per_pipeline": dict(per_pipeline)}


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


def derive_ci_failure_coalesce_key(raw_payload: dict[str, Any] | None) -> str | None:
    """Derive a stable coalesce key for CI-failure (Branch Fixer) webhook deliveries.

    The GitHub Actions ``ci.yml`` ``notify-pr-failure`` job dispatches the Branch
    Fixer with a payload shaped ``{branchName, prNumber, runUrl,
    failureDescription, headSha}``. ``runUrl`` is the GitHub run id of the CI
    workflow that failed and therefore differs on EVERY delivery, so the
    canonical payload hash (FAR-1034) could never deduplicate two CI-failure
    events for the same PR — each compared unequal and minted its own full
    Branch Fixer run.

    This key pins the identity a duplicate really has: the PR number plus the
    head SHA of the commit whose CI failed. Two events for the same PR at the
    same head SHA (re-triggered CI, re-opened PR, a second workflow run for the
    same commit) produce the SAME key and are coalesced; a new commit changes
    the head SHA and is never suppressed. Shape: ``ci-failure:pr:<n>:sha:<sha>``.

    Returns ``None`` (no coalescing) for anything that is not a CI-failure
    delivery — the merge-queue conflict / migration-collision payloads carry
    ``phase`` and no ``prNumber``, and a manual ``branch-fixer.yml`` dispatch has
    no ``headSha``, so an intentionally-fired fix is never suppressed.
    """
    if not isinstance(raw_payload, dict):
        return None
    pr_number = raw_payload.get("prNumber")
    head_sha = raw_payload.get("headSha")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int):
        return None
    if isinstance(head_sha, bool) or not isinstance(head_sha, str) or not head_sha:
        return None
    return f"ci-failure:pr:{pr_number}:sha:{head_sha}"


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
