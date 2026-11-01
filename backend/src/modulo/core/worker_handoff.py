"""Graceful worker-shutdown run handoff (FAR-1627).

Every ``fly deploy`` to the single prod worker SIGTERMs the SAQ runs worker
while agent runs (tens of minutes long) are mid-execution. Before this module,
nothing noticed at shutdown: the executor died, the run row stayed ``running``
with a frozen heartbeat, and detection waited out the heartbeat TTL
(``SLOT_RECONCILE_STALE_SECONDS`` = 30 min) — at which point the sweep
force-released the row with a code that misattributed the loss (the FAR-1603
RCA: 10 of 19 releases over 3 days killed in-flight runs, surfaced 30-35 min
later as ``harness.dispatch_failed`` / ``never_dispatched``).

The handoff: the shutting-down worker, from the SAQ ``shutdown`` lifecycle
hook (``saq_worker._shutdown_handoff_hook``), promptly writes the loss for
every run it still owns. Each run takes ONE of two paths, decided by the gates
below — either the claim is released (row reset to ``pending``, so
``dispatcher_reconcile`` re-dispatches on its next 60s tick), or the run is
terminal-failed right there with the same truthful ``harness.worker_lost``
code when a re-run is NOT safe. Either way the operator reads "the worker
died" in seconds instead of a misleading dispatch-path code 30-35 minutes
later. The terminal arm additionally books the run's elapsed sandbox
occupancy as derived spend (FAR-2077): the raw terminal writer records no cost
columns, so without it hours of sandbox time reach the ledger as ``null``.

Two gates decide which path a run takes, and neither is optional:

* CANCELLATION OBSERVATION. The hook cannot assume the predecessor executor is
  dead: SAQ runs the ``shutdown`` callbacks unconditionally after
  ``cancel_tasks``, INCLUDING when cancellation timed out (``cancel_tasks``
  returns only ``all(task.done())``; ``Worker.stop()`` logs "Some tasks did
  not finish cancellation in time…" and proceeds). So an entry is handed back
  ONLY when its task actually OBSERVED the ``asyncio.CancelledError``. That
  observation is strong evidence the predecessor is gone: the exception only
  reaches :func:`track_inflight_run`'s handler after
  ``pipeline_execution._await_executor_task``'s ``finally`` has cancelled AND
  awaited the executor task, the heartbeat loop and both watchdogs
  (``_cancel_and_await_tasks``), so the whole per-run task set is drained —
  and the ``async with`` body (every frame of ``execute_run`` below the
  claim scope) has unwound — before the entry is marked. Residual race,
  documented rather than removed: SAQ can cancel a task TWICE (the
  grace-period ``wait_for`` cancels it, then ``cancel_tasks`` cancels it
  again), and a second cancellation landing while that ``finally`` drain is
  in flight can cut the drain short — leaving the executor task in its
  TEARDOWN frames (never node execution: the body has already unwound) when
  the hook runs. What is never handed back is an entry whose task never
  observed the cancellation at all — a task stuck before its first await
  point, or still unwinding when the hard deadline expired — because that is
  exactly the case where a predecessor could still be executing nodes. Such a
  run is left untouched and falls back to the heartbeat-TTL sweeps, so the
  failure mode is always "wait for the backstop", never "re-dispatch against
  a live predecessor".
* IDEMPOTENCY + RETRY POLICY (the requeue gate). The reset puts the row on
  ``dispatcher_reconcile``'s capacity-deferred leg (``pending`` +
  ``dispatched_at IS NULL``), which re-dispatches as ``execute_run`` — a
  RE-EXECUTION of the run, not a checkpoint resume. That leg is un-gated (no
  idempotency check of its own; see ``executor._fenced_pending_reset``'s
  contract: "A new call site MUST keep that gate or a non-idempotent
  node-started run becomes re-dispatchable"), so the gate lives HERE. A
  node-started run is re-queued only when the graph is idempotent (FAR-295)
  AND the pipeline's ``retry_policy`` does not forbid the re-run; otherwise it
  is terminal-failed in place. A genuinely pre-node run (the shared
  ``cron_helpers._is_nodeless_zombie_row`` zero-node premise, re-checked row
  side — checkpoints / ``__final__`` output / live dispatch marker) keeps the
  exact sibling decision ``_should_redispatch_nodeless``: budget-default
  repair within ``SAQ_NODELESS_REDISPATCH_BUDGET``, terminal-fail when an
  explicit policy covers neither the stall nor the failure class. Both
  terminal outcomes are the same terminal state the age backstop would
  eventually reach (~2 hours later, as a misattributed ``no_progress``),
  reached promptly and truthfully instead.

Re-queueing a node-started run re-runs the interrupted node (E2B sandbox work
already done is lost unless re-attached) — the ticket's sanctioned outcome
("re-dispatched within the ~1-minute window if loss is unavoidable"). Per-
pipeline control of that requeue (default ``fail``) is FAR-1626's safety net,
not this module's; the handback itself must happen promptly regardless.

Never raises into the shutdown path (except ``CancelledError``): a failed
handback is logged loudly and the run falls back to the pre-existing
heartbeat-TTL sweeps — the handoff is a promptness improvement over a
30-minute backstop, never a correctness dependency.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, cast, exists, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.db.sqlstates import is_row_lock_timeout

_log = logging.getLogger(__name__)

#: Canonical (registered) error code the handback stamps. Dotted registry
#: spelling — passes ``map_legacy_code`` through unchanged, never resolves via
#: the legacy ``worker_lost`` -> ``harness.dispatch_failed`` alias (which would
#: resurrect exactly the misattribution FAR-1627 removes).
WORKER_LOST_ERROR_CODE = "harness.worker_lost"

#: Synthetic error detail stamped on every handed-back run: names the shutdown
#: and the handback so the operator reads the truth, not a dispatch-path
#: generic.
WORKER_LOST_DETAIL = (
    "Worker executing this run was shut down (deploy or restart); the claim was "
    "released and the run handed back for re-dispatch (FAR-1627)."
)

#: Detail stamped on a run the handback TERMINAL-FAILS instead of re-queuing
#: (the requeue gate refused a safe re-run). Same truthful worker-loss prefix,
#: plus the machine-checked reason — the operator learns both WHAT happened
#: and WHY it was not retried, from the row alone.
WORKER_LOST_NOT_REQUEUED_DETAIL = (
    "Worker executing this run was shut down (deploy or restart); the run was "
    "terminal-failed instead of handed back for re-dispatch because a re-run is "
    "not safe (FAR-1627). Reason: "
)

# RLS set_config helper — same contract as the periodic sweeps (the per-org
# statement runs inside set_config('app.organisation_id', ...)).
_SQL_SET_ORG_ID = "SELECT set_config('app.organisation_id', :val, true)"


def _engine_evidence_expr(column: Any) -> Any:
    """Return a PostgreSQL expression for engine-owned evidence in one blob.

    ``outputs_json`` and ``node_telemetry_json`` are maps of node id to an
    envelope.  FAR-1780 stamps only agent-supplied datums in each envelope's
    ``provenance`` map as ``reported``; every unstamped datum is engine-owned
    (observed or derived).  A legacy envelope has no map, so its non-provenance
    keys deliberately remain evidence.  The expression therefore avoids
    substring or field-name heuristics and follows the persisted datum map.
    """
    node_entries = (
        func.jsonb_each(cast(column, JSONB)).table_valued("node_id", "node_value").render_derived(name="node_entries")
    )
    node_value = node_entries.c.node_value
    datum_keys = func.jsonb_object_keys(node_value).table_valued("datum_keys").render_derived(name="datum_keys")
    datum_key = datum_keys.c.datum_keys
    provenance_value = node_value.op("->")(literal("provenance")).op("->>")(datum_key)
    engine_datum = exists(
        select(1)
        .select_from(datum_keys)
        .where(
            datum_key != literal("provenance"),
            provenance_value.is_distinct_from(literal("reported")),
        )
    )
    node_has_evidence = or_(
        func.jsonb_typeof(node_value).is_distinct_from(literal("object")),
        engine_datum,
    )
    return or_(
        and_(column.is_not(None), func.jsonb_typeof(column).is_distinct_from(literal("object"))),
        exists(select(1).select_from(node_entries).where(node_has_evidence)),
    )


# The handback RE-QUEUE arm. Fenced exactly like every other claim-fenced
# writer:
#   * ``status = 'running'`` — only a run still marked running is handed back
#     (a run whose fate already landed — complete / failed / cancelled — is
#     untouched, rowcount 0).
#   * ``claim_token = :tok`` — the registry entry's token; a successor that
#     re-claimed the run between registration and handback rotated the token,
#     so this row is no longer ours to release (fence-out, never clobber).
#   * ``cancellation_requested = false`` — cancel wins; the cancel path owns
#     the row's status.
#   * ``dispatch_phase IS DISTINCT FROM 'first_node_dispatched' OR
#     :node_start_safe`` — the M1 belt on top of the Python decision below:
#     the capacity-deferred re-dispatch leg this reset feeds is UNGATED on
#     graph idempotency (it re-dispatches as ``execute_run``, a re-EXECUTION),
#     so a node-started row may only be reset when its graph was vetted safe
#     to re-run. The belt is evaluated against the ROW AT UPDATE TIME, so a
#     phase that advanced after the decision read can never slip through.
# Reset mirrors the heartbeat-stale slot sweep's retry arm (FAR-779/812):
# ``pending`` + cleared ``dispatched_at`` / ``dispatcher`` / ``heartbeat_at``
# so the ``dispatcher_reconcile`` pending legs (capacity-deferred:
# ``pending`` + ``dispatched_at IS NULL``) admit the row on the next 60s tick.
# ``error_code``/``error_detail`` are STAMPED (not cleared) — the reset state
# must carry the truthful worker-loss code (the re-claim's
# ``clear_error_code`` clears it when the run is picked back up).
_HANDBACK_RESET_SQL = text(
    "UPDATE runs SET status='pending', error_code=:code, error_detail=:detail, "  # nosec B608
    "completed_at=NULL, dispatched_at=NULL, dispatcher=NULL, heartbeat_at=NULL "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND status='running' "
    "AND claim_token=:tok "
    "AND cancellation_requested=false "
    "AND (dispatch_phase IS DISTINCT FROM :first_node OR :node_start_safe) "
    "RETURNING id"
)


@dataclass(frozen=True)
class InFlightRun:
    """One run this worker process claimed and is still executing."""

    run_id: uuid.UUID
    org_id: uuid.UUID
    claim_token: str
    #: True once the owning TASK observed ``asyncio.CancelledError`` (SAQ's
    #: shutdown cancellation tore through :func:`track_inflight_run`). Only
    #: observed entries are handed back — see the module docstring's
    #: cancellation-observation gate. A fresh claim starts ``False``; SAQ's
    #: shutdown cancellation flips it just before the task re-raises.
    cancellation_observed: bool = False


@dataclass(frozen=True)
class _HandbackDecision:
    """Requeue-vs-terminal decision for one observed handback entry (M1)."""

    #: True -> the claim is released (reset to ``pending`` for re-dispatch);
    #: False -> the run is terminal-failed in place with ``reason``.
    may_requeue: bool
    #: Belt parameter for the reset arm: would a NODE-STARTED row be safe to
    #: re-queue? (graph idempotent AND the node-started retry-policy veto
    #: passed). Evaluated on the row at UPDATE time — see ``_HANDBACK_RESET_SQL``.
    node_start_safe: bool
    #: Human-readable reason when ``may_requeue`` is False ("" otherwise).
    reason: str


class InFlightRunRegistry:
    """Process-local map of runs currently executing in THIS worker process.

    Keyed by ``run_id`` (a SAQ retry re-invokes ``execute_run`` and re-claims
    with a FRESH token — the re-claim replaces the entry, so the latest claim
    always wins). Registration happens right after a successful claim;
    deregistration happens ONLY on paths where the job function returns
    normally (the run's fate — complete / failed / awaiting_human / not
    claimed — was decided and written). ``asyncio.CancelledError`` (SAQ's
    shutdown cancellation) deliberately does NOT deregister: an abandoned run
    is exactly what the handback must see — it only records that the task
    OBSERVED the cancellation (M3's gate).

    Both deregistration and the observation flip are CLAIM-TOKEN fenced (M2):
    SAQ's cancellation branch can re-enqueue a run while the previous attempt
    is still exiting, so an older attempt must never be able to delete (or
    mark) the newer attempt's entry.
    """

    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, InFlightRun] = {}
        self._lock = threading.Lock()

    def register(self, *, run_id: uuid.UUID, org_id: uuid.UUID, claim_token: str) -> None:
        with self._lock:
            self._runs[run_id] = InFlightRun(run_id=run_id, org_id=org_id, claim_token=claim_token)

    def mark_cancellation_observed(self, run_id: uuid.UUID, claim_token: str) -> None:
        """Record that THIS attempt's task observed the cancellation (M3).

        Token-fenced for the same reason :meth:`discard` is: a superseded
        attempt's cancellation must not mark the successor's fresh entry as
        observed (that successor may still be running — handback would then
        release a live executor's run).
        """
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or entry.claim_token != claim_token:
                return
            self._runs[run_id] = replace(entry, cancellation_observed=True)

    def discard(self, run_id: uuid.UUID, claim_token: str) -> None:
        """Drop THIS attempt's entry — only when its token still owns it (M2).

        An unconditional pop lets an overlapping older attempt's exit delete
        the newer attempt's entry, so a later shutdown has no entry for that
        run and it waits out the 30-minute heartbeat TTL — the exact failure
        FAR-1627 removes. A stale token, an already-replaced entry, and an
        unknown id are all silent no-ops (idempotency).
        """
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None and entry.claim_token == claim_token:
                del self._runs[run_id]

    def snapshot(self) -> list[InFlightRun]:
        with self._lock:
            return list(self._runs.values())

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._runs)


_PROCESS_REGISTRY = InFlightRunRegistry()


def get_inflight_registry() -> InFlightRunRegistry:
    """The process-wide in-flight registry (one per worker process)."""
    return _PROCESS_REGISTRY


@asynccontextmanager
async def track_inflight_run(
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    claim_token: str,
    registry: InFlightRunRegistry | None = None,
) -> AsyncIterator[None]:
    """Scope one claimed run's ownership for the shutdown handoff (FAR-1627).

    Registers the run on entry and deregisters it on EVERY exit EXCEPT
    ``asyncio.CancelledError`` — SAQ's shutdown cancellation abandons the
    executor mid-run, and an abandoned run is exactly what
    :func:`handback_inflight_runs` must see. On that cancellation the entry is
    additionally marked OBSERVED: the exception reaching this handler proves
    everything inside the ``async with`` (the executor's body) has unwound, so
    the run is safe to hand back (the module docstring's M3 gate). Any other
    exception means the job function failed and SAQ's ``after_process`` hook
    (or a terminal writer in the unwinding stack) owns the run's fate, so the
    entry is dropped.
    """
    reg = registry if registry is not None else get_inflight_registry()
    reg.register(run_id=run_id, org_id=org_id, claim_token=claim_token)
    try:
        yield
    except asyncio.CancelledError:
        # Keep the registry entry and record the observation: the shutdown
        # handback releases this run's claim once the cancellation completes
        # (SAQ stop() runs the shutdown callback after the cancellation hard
        # deadline). Token-fenced — a superseded attempt's cancellation must
        # not mark the successor's entry.
        reg.mark_cancellation_observed(run_id, claim_token)
        raise
    except BaseException:
        reg.discard(run_id, claim_token)
        raise
    else:
        reg.discard(run_id, claim_token)


async def _load_handback_row(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[Any, Any] | None:
    """Load one run's requeue-gate inputs (row-like + graph), or ``None``.

    The row-like object carries EXACTLY the attributes the shared sibling
    readers consume (``cron_helpers._is_nodeless_zombie_row`` /
    ``_should_redispatch_nodeless``), with ``outputs_absent`` /
    ``checkpoints_absent`` built from the ONE shared NOT-EXISTS expressions
    those predicates use — parity by construction, so the zero-node premise
    rechecked here cannot drift from the reconcile's. Returns ``(row,
    graph_json)``; ``None`` when the run (or its snapshot) is not visible —
    fail closed: an entry with no evidence is skipped, never re-queued.
    """
    from modulo.core.cron_helpers import _checkpoint_absent_expr, _final_row_absent_expr
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.run import Run
    from modulo.db.models.run_node_outputs import RunNodeOutput

    observed_activity = exists(
        select(1).where(
            RunNodeOutput.run_id == Run.id,
            RunNodeOutput.organisation_id == org_id,
            or_(
                _engine_evidence_expr(RunNodeOutput.node_telemetry_json),
                and_(
                    RunNodeOutput.node_telemetry_json.is_(None),
                    _engine_evidence_expr(RunNodeOutput.outputs_json),
                ),
                RunNodeOutput.raw_output_markers.is_not(None),
                RunNodeOutput.artifacts_json.is_not(None),
                RunNodeOutput.schema_enforcement_json.is_not(None),
            ),
        )
    ).label("observed_activity")

    stmt = (
        select(
            Run.status,
            Run.dispatch_phase,
            Run.node_attempt_count,
            # FAR-2076: the ONE execution measure. The handback's retry budget
            # compares against this, never against claim_count (which grows on
            # capacity-demoted and resume claims — and is not selected at all,
            # because no reader consumes it).
            Run.execution_attempt_count,
            Run.node_token_usage,
            Run.started_at,
            Run.dispatched_at,
            Run.sandbox_dispatch_state,
            Pipeline.retry_policy,
            PipelineSnapshot.graph_json,
            observed_activity,
            _final_row_absent_expr().label("outputs_absent"),
            _checkpoint_absent_expr().label("checkpoints_absent"),
        )
        .join(PipelineSnapshot, PipelineSnapshot.id == Run.snapshot_id)
        .outerjoin(Pipeline, Pipeline.id == Run.pipeline_id)
        .where(Run.id == run_id, Run.organisation_id == org_id)
    )
    values = (await conn.execute(stmt)).mappings().one_or_none()
    if values is None:
        return None
    row = SimpleNamespace(
        status=values["status"],
        dispatch_phase=values["dispatch_phase"],
        node_attempt_count=values["node_attempt_count"],
        execution_attempt_count=values["execution_attempt_count"],
        node_token_usage=values["node_token_usage"],
        started_at=values["started_at"],
        dispatched_at=values["dispatched_at"],
        sandbox_dispatch_state=values["sandbox_dispatch_state"],
        retry_policy=values["retry_policy"],
        observed_activity=bool(values["observed_activity"]),
        outputs_absent=bool(values["outputs_absent"]),
        checkpoints_absent=bool(values["checkpoints_absent"]),
    )
    return row, values["graph_json"]


def _node_started_policy_allows(row: Any) -> tuple[bool, str]:
    """Does the pipeline ``retry_policy`` permit a worker-loss re-run (M1)?

    Never-retry veto, matching the sibling legs' vocabulary (FAR-733/1088):
    an absent/malformed policy, or an explicit empty ``on`` (the ``{}``
    column default and the FAR-525 GUI's inert no-op panel save), is NOT a
    never-retry decision — it falls through to the handback's sanctioned
    default requeue (FAR-1626's per-pipeline control is not implemented yet).
    A NON-EMPTY ``on`` that covers neither the failure class (the handback's
    own terminal spelling, ``harness.worker_lost``) nor the stall class (the
    executor-death outcome the nodeless sibling maps to) forbids the re-run,
    and a covered policy still has to have budget left — the shared
    :func:`modulo.core.retry_budget.retry_executions_consumed` bound, fed from
    the durable ``runs.execution_attempt_count`` (FAR-2076). That counter is
    the number of EXECUTION attempts of the graph, so it also bounds a
    PRE-node death (worker lost during hub init / graph compile) without
    counting a capacity-demoted or resume claim as an execution.
    """
    from modulo.core.pipeline_engine.executor import _retry_after_policy
    from modulo.core.retry_budget import retry_executions_consumed

    policy = getattr(row, "retry_policy", None)
    if not isinstance(policy, dict) or not policy:
        return True, ""
    on = policy.get("on")
    if not (isinstance(on, list) and on):
        return True, ""
    budget = _retry_after_policy(policy, "failed", WORKER_LOST_ERROR_CODE)
    if budget is None:
        budget = _retry_after_policy(policy, "stalled", "executor_stalled")
    if budget is None:
        return False, "the pipeline retry policy covers neither the failure nor the stall class"
    attempt = retry_executions_consumed(
        execution_attempt_count=getattr(row, "execution_attempt_count", 0),
    )
    if attempt > budget:
        return False, f"retry budget exhausted (attempt {attempt} > max_retries {budget})"
    return True, ""


def _handback_decision(row: Any, graph_json: Any) -> _HandbackDecision:
    """Decide re-queue vs terminal-fail for one observed handback entry (M1).

    The two gates from the module docstring, in order:

    1. ZERO-NODE PREMISE — ``cron_helpers._is_nodeless_zombie_row`` (the
       shared row-level recheck: status, ``node_token_usage``, ``outputs_absent``,
       durable phase, ``checkpoints_absent``, live dispatch marker). ``age_minutes=0``
       because a shutdown hands back runs of ANY age: zero makes the age leg
       non-binding while every premise leg still applies. A row that passes
       keeps the EXACT sibling decision ``_should_redispatch_nodeless``
       (retry budget / never-retry policy).
    2. NODE-STARTED (or premise unprovable — fail toward the stricter gate):
       re-queue only when the graph is idempotent (FAR-295) AND the retry
       policy allows the re-run; otherwise terminal-fail in place.
    """
    from modulo.core.cron_helpers import _is_nodeless_zombie_row, _should_redispatch_nodeless
    from modulo.core.pipeline_engine.executor import _graph_is_idempotent

    graph = graph_json if isinstance(graph_json, dict) else None
    graph_idempotent = _graph_is_idempotent(graph)
    policy_ok, policy_reason = _node_started_policy_allows(row)
    node_start_safe = graph_idempotent and policy_ok

    # Durable evidence wins before every requeue arm, including a row that
    # otherwise satisfies the zero-node premise. Re-running after any observed
    # or derived activity can repeat side effects and discard that evidence.
    if getattr(row, "observed_activity", False):
        return _HandbackDecision(
            may_requeue=False,
            node_start_safe=False,
            reason="durable sandbox activity was observed; terminalisation is safer than re-execution",
        )

    # age_minutes=0 disables ONLY the age leg (a shutdown hands back runs of
    # ANY age); the in-flight shield leg inside _is_nodeless_zombie_row stays
    # active, so a still-dispatching row is never handed back.
    if _is_nodeless_zombie_row(row, 0):
        if _should_redispatch_nodeless(row):
            return _HandbackDecision(may_requeue=True, node_start_safe=node_start_safe, reason="")
        return _HandbackDecision(
            may_requeue=False,
            node_start_safe=node_start_safe,
            reason="zero-node retry budget / retry policy excludes a re-dispatch",
        )

    if not graph_idempotent:
        return _HandbackDecision(
            may_requeue=False,
            node_start_safe=False,
            reason="the pipeline graph contains a non-idempotent node (FAR-295)",
        )
    if not policy_ok:
        return _HandbackDecision(may_requeue=False, node_start_safe=False, reason=policy_reason)
    return _HandbackDecision(may_requeue=True, node_start_safe=node_start_safe, reason="")


async def _handback_one_entry(
    conn: AsyncConnection,
    entry: InFlightRun,
    org_id: uuid.UUID,
) -> tuple[int, int, str | None]:
    """Hand one observed entry back inside the org transaction — or refuse it.

    Runs the requeue gate (:func:`_handback_decision`) and, when it permits,
    issues the fenced reset (:data:`_HANDBACK_RESET_SQL`). Returns
    ``(handed_back, skipped, terminal_reason)``: a non-refused entry contributes
    exactly 1 to one of the first two, while ``terminal_reason`` is non-None
    when the gate refused a safe re-run — the caller terminal-fails that run
    OUTSIDE this transaction (its own bounded-lock write).
    """
    loaded = await _load_handback_row(conn, run_id=entry.run_id, org_id=org_id)
    if loaded is None:
        _log.info(
            "worker_handoff.skipped run=%s org=%s (run row not visible under this org)",
            entry.run_id,
            org_id,
        )
        return 0, 1, None
    row, graph_json = loaded
    decision = _handback_decision(row, graph_json)
    if not decision.may_requeue:
        return 0, 0, decision.reason
    result = await conn.execute(
        _HANDBACK_RESET_SQL,
        {
            "rid": str(entry.run_id),
            "oid": str(org_id),
            "tok": entry.claim_token,
            "code": WORKER_LOST_ERROR_CODE,
            "detail": WORKER_LOST_DETAIL,
            "first_node": PHASE_FIRST_NODE_DISPATCHED,
            "node_start_safe": decision.node_start_safe,
        },
    )
    if result.fetchone() is not None:
        _log.warning(
            "worker_handoff.released run=%s org=%s (worker shutdown mid-run; reset to pending with %s for re-dispatch)",
            entry.run_id,
            org_id,
            WORKER_LOST_ERROR_CODE,
        )
        return 1, 0, None
    _log.info(
        "worker_handoff.skipped run=%s org=%s "
        "(fenced out: fate already landed, claim rotated, or cancellation requested)",
        entry.run_id,
        org_id,
    )
    return 0, 1, None


async def _handback_org(
    async_engine: AsyncEngine,
    org_id: uuid.UUID,
    org_entries: list[InFlightRun],
) -> tuple[int, int, list[tuple[InFlightRun, str]], list[InFlightRun]]:
    """One org's RLS-scoped transaction: reads + resets, returning its totals.

    Row-lock bounded (FAR-1592/1584 pattern shared with the periodic sweeps): a
    55P03 row-lock-timeout expiry — or any other per-org failure — rolled the
    transaction back whole, so NONE of its runs moved. It is counted as skipped
    and the remaining orgs still run.

    Returns ``(handed_back, skipped, terminals, handed_back_entries)``. The
    entries handed back are returned so their FAR-2078 attempt rows can be
    closed AFTER this transaction commits — closing them here would open a
    second transaction while this one holds the run rows' locks.
    """
    from modulo.db.crud.row_lock import set_mutation_row_lock_timeout

    handed_back = 0
    skipped = 0
    terminals: list[tuple[InFlightRun, str]] = []
    handed_back_entries: list[InFlightRun] = []
    try:
        async with async_engine.connect() as conn, conn.begin():
            await set_mutation_row_lock_timeout(conn)
            await conn.execute(text(_SQL_SET_ORG_ID), {"val": str(org_id)})
            for entry in org_entries:
                entry_handed_back, entry_skipped, reason = await _handback_one_entry(conn, entry, org_id)
                handed_back += entry_handed_back
                skipped += entry_skipped
                if entry_handed_back:
                    handed_back_entries.append(entry)
                if reason is not None:
                    terminals.append((entry, reason))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if is_row_lock_timeout(exc):
            # FAR-1592 shape: the org transaction rolled back whole —
            # NONE of its runs moved; they stay ``running`` and the
            # heartbeat-TTL sweeps remain the backstop. WARNING, skip,
            # continue with the remaining orgs.
            _log.warning(
                "worker_handoff.org_lock_timeout org=%s entries=%d "
                "(SQLSTATE 55P03 from the bounded mutation_row_lock_timeout_ms wait) — "
                "org transaction rolled back with NO runs released; they fall back to the "
                "heartbeat-TTL sweeps",
                org_id,
                len(org_entries),
                exc_info=True,
            )
        else:
            _log.exception(
                "worker_handoff.org_handback_failed org=%s entries=%d (falls back to the heartbeat-TTL sweeps)",
                org_id,
                len(org_entries),
            )
        return 0, len(org_entries), [], []
    return handed_back, skipped, terminals, handed_back_entries


async def _close_handed_back_attempts(
    async_engine: AsyncEngine,
    entries: list[InFlightRun],
) -> None:
    """Close each handed-back run's open attempt as ``worker_lost`` (FAR-2078).

    Runs AFTER the org transaction commits: the attempt rows are the durable
    record of the attempt this reset just ended, and the reset erased the
    reason from ``runs.error_code``. FAIL-OPEN — the handback already committed,
    so a diagnostics write must never affect it (``record_attempt_close``
    swallows and logs every failure).
    """
    if not entries:
        return
    from modulo.core.pipeline_execution import engine_session_factory, record_attempt_close
    from modulo.db.models.run_attempt import ATTEMPT_TERMINAL_REASON_WORKER_LOST

    factory = engine_session_factory(async_engine)
    for entry in entries:
        await record_attempt_close(
            factory,
            entry.run_id,
            entry.org_id,
            terminal_reason=ATTEMPT_TERMINAL_REASON_WORKER_LOST,
            error_code=WORKER_LOST_ERROR_CODE,
        )


async def _terminalise_entry(
    async_engine: AsyncEngine,
    entry: InFlightRun,
    reason: str,
) -> tuple[int, int]:
    """Terminal-fail one run the gate refused to re-queue (its own transaction).

    Used when the requeue gate refuses a safe re-run — the same terminal state
    the age backstop would reach ~2h later, only prompt and correctly
    attributed. Returns ``(terminalised, skipped)``: a 55P03 from
    ``fail_run_terminal``'s own bounded lock (or any other write error) counts
    the run as skipped and continues — it stays ``running`` and the sweeps
    remain the backstop.

    FAR-2077: the raw terminal write records no cost columns, so after a write
    that LANDED the run's elapsed sandbox occupancy is booked as derived spend
    (``record_worker_lost_derived_cost``). A fenced-out write books nothing —
    the row's fate belongs to another writer.
    """
    from modulo.core.pipeline_execution import fail_run_terminal, record_worker_lost_derived_cost

    try:
        ok = await fail_run_terminal(
            async_engine,
            str(entry.run_id),
            str(entry.org_id),
            error_code=WORKER_LOST_ERROR_CODE,
            error_detail=f"{WORKER_LOST_NOT_REQUEUED_DETAIL}{reason}.",
            claim_token=entry.claim_token,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "worker_handoff.terminal_failed_write_error run=%s org=%s "
            "(stays running; falls back to the heartbeat-TTL sweeps)",
            entry.run_id,
            entry.org_id,
        )
        return 0, 1
    if ok:
        _log.warning(
            "worker_handoff.terminalised run=%s org=%s reason=%s "
            "(worker shutdown mid-run; re-run not safe, failed in place with %s)",
            entry.run_id,
            entry.org_id,
            reason,
            WORKER_LOST_ERROR_CODE,
        )
        # FAR-2077: the raw terminal write above records no cost columns, so
        # book the run's elapsed sandbox occupancy as derived spend. Only after
        # a terminal write that LANDED (a fenced-out row belongs to another
        # writer). Fail-open inside the helper — the terminal write already
        # committed and must never be undone by its cost booking.
        await record_worker_lost_derived_cost(
            async_engine,
            str(entry.run_id),
            str(entry.org_id),
            error_code=WORKER_LOST_ERROR_CODE,
            error_detail=f"{WORKER_LOST_NOT_REQUEUED_DETAIL}{reason}.",
        )
        return 1, 0
    _log.info(
        "worker_handoff.terminal_skipped run=%s org=%s "
        "(fenced out: fate already landed, claim rotated, or cancellation requested)",
        entry.run_id,
        entry.org_id,
    )
    return 0, 1


async def _terminalise_org(
    async_engine: AsyncEngine,
    terminals: list[tuple[InFlightRun, str]],
) -> tuple[int, int]:
    """Terminal-fail every refused entry for one committed org transaction."""
    terminalised = 0
    skipped = 0
    for entry, reason in terminals:
        entry_terminalised, entry_skipped = await _terminalise_entry(async_engine, entry, reason)
        terminalised += entry_terminalised
        skipped += entry_skipped
    return terminalised, skipped


async def handback_inflight_runs(
    async_engine: AsyncEngine,
    *,
    registry: InFlightRunRegistry | None = None,
) -> dict[str, int]:
    """Hand every still-registered in-flight run back — or fail it truthfully.

    Per observed entry the requeue gate (:func:`_handback_decision`) picks one
    of two fenced writes:

    * RE-QUEUE (:data:`_HANDBACK_RESET_SQL`, reset to ``pending`` so
      ``dispatcher_reconcile`` re-dispatches on its next 60s tick), or
    * TERMINAL-FAIL via ``pipeline_execution.fail_run_terminal`` (claim-token
      fenced, ``status='failed'`` + this module's truthful worker-loss code,
      with the journey-advance / daily-fact ceremonies every other raw
      terminal writer runs). Used when the gate refuses a safe re-run — the
      same terminal state the age backstop would reach ~2h later, only prompt
      and correctly attributed.

    Per org (RLS-scoped, row-lock bounded — FAR-1592/1584 pattern shared with
    the periodic sweeps): one transaction of reads + resets. A 55P03
    row-lock-timeout expiry is NOT a failure for the org — its transaction
    rolled back whole, so none of its moves persisted; those runs stay
    ``running`` and fall back to the heartbeat-TTL sweeps (WARNING + skip,
    never a lost recovery). Every other per-org failure is logged loudly and
    the remaining orgs still run. Entries whose task never OBSERVED the
    cancellation are never touched at all (the M3 gate) — a stuck predecessor
    is not provably gone, so its run waits for the backstop rather than being
    re-dispatched against a live executor.

    The registry is drained at the end (success or failure): the process is
    shutting down, and a stale entry must never be handed back twice.

    Never raises (except ``CancelledError``) — this runs inside the SAQ
    ``shutdown`` lifecycle hook, where an exception would surface as a worker
    crash instead of a logged, backstopped degradation.

    Returns ``{"handed_back": int, "terminalised": int, "skipped": int}`` —
    ``handed_back`` counts rows the reset moved to ``pending``;
    ``terminalised`` counts rows the gate failed in place;
    ``skipped`` counts everything left untouched (fate already landed, token
    rotated, cancellation requested, predecessor never observed the
    cancellation, or a per-org failure).
    """
    reg = registry if registry is not None else get_inflight_registry()
    entries = reg.snapshot()
    if not entries:
        return {"handed_back": 0, "terminalised": 0, "skipped": 0}

    # M3 gate: only entries whose task actually observed SAQ's cancellation
    # are candidates — everything else means the predecessor is not provably
    # gone (see the module docstring).
    observed = [entry for entry in entries if entry.cancellation_observed]
    unobserved = len(entries) - len(observed)
    if unobserved:
        _log.warning(
            "worker_handoff.not_observed entries=%d "
            "(task never observed SAQ cancellation — predecessor not provably dead; "
            "runs left to the heartbeat-TTL sweeps)",
            unobserved,
        )

    by_org: dict[uuid.UUID, list[InFlightRun]] = {}
    for entry in observed:
        by_org.setdefault(entry.org_id, []).append(entry)

    handed_back = 0
    terminalised = 0
    skipped = unobserved
    try:
        for org_id, org_entries in by_org.items():
            org_handed_back, org_skipped, terminals, handed_back_entries = await _handback_org(
                async_engine, org_id, org_entries
            )
            handed_back += org_handed_back
            skipped += org_skipped
            # The org transaction COMMITTED — close the attempts it ended (the
            # FAR-2078 per-attempt history; the reset erased the reason from
            # ``runs.error_code``), then run the terminal arm for the rows the
            # gate refused to re-queue.
            await _close_handed_back_attempts(async_engine, handed_back_entries)
            org_terminalised, org_term_skipped = await _terminalise_org(async_engine, terminals)
            terminalised += org_terminalised
            skipped += org_term_skipped
    finally:
        reg.clear()

    if handed_back or terminalised or skipped:
        _log.warning(
            "worker_handoff.sweep_complete handed_back=%d terminalised=%d skipped=%d orgs=%d",
            handed_back,
            terminalised,
            skipped,
            len(by_org),
        )
    return {"handed_back": handed_back, "terminalised": terminalised, "skipped": skipped}
