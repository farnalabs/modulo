"""Recovery handler for failed manual-input nodes.

Provides the core logic to replay or skip a manual node that failed or
is awaiting human input (``POST /recover``), plus the guardrail-override
remediation for guardrail-blocked terminal runs (``POST /guardrail-override``).
A guardrail-blocked run (``eval_failed``/``eval_blocked``) is REFUSED by the
generic recovery path — the override endpoint is the only remediation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.core.audit_logger.labels import SYSTEM_ACTOR
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.run_provenance import run_provenance_fields
from modulo.db.crud.run import POLICY_GATE_PIN_MISMATCH_MARKER, _input_hash, get_run
from modulo.db.crud.run_node_outputs import read_run_blobs
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

_log = logging.getLogger(__name__)


class RecoveryNotAllowedError(RuntimeError):
    """Raised when the run state does not permit recovery."""

    def __init__(self, run_id: uuid.UUID, status: str) -> None:
        super().__init__(f"Run {run_id} is in status {status!r} — recovery requires 'failed' or 'awaiting_human'")
        self.run_id = run_id
        self.status = status


class NodeNotFoundInGraphError(KeyError):
    """Raised when the node_id does not exist in the pipeline graph."""

    def __init__(self, run_id: uuid.UUID, node_id: str) -> None:
        super().__init__(f"Node {node_id!r} not found in graph for run {run_id}")
        self.run_id = run_id
        self.node_id = node_id


class NodeAlreadyCompletedError(RuntimeError):
    """Raised when attempting to recover a node that has already completed."""

    def __init__(self, run_id: uuid.UUID, node_id: str) -> None:
        super().__init__(f"Node {node_id!r} on run {run_id} has already completed — recovery not allowed")
        self.run_id = run_id
        self.node_id = node_id


class ConcurrentRecoveryError(RuntimeError):
    """Raised when another recovery attempt wins a concurrent race."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"Concurrent recovery attempt detected for run {run_id}")
        self.run_id = run_id


class GuardrailOverrideError(RuntimeError):
    """Raised when a guardrail override is not permitted for the run."""

    def __init__(self, run_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"Guardrail override refused for run {run_id}: {reason}")
        self.run_id = run_id
        self.reason = reason


class GuardrailOverrideRejectedError(GuardrailOverrideError):
    """Raised when the override's supplied input still violates a guardrail.

    The override re-runs the guardrail pass on the operator-supplied input
    (re-block safe default) — a still-violating input never flips the run back
    to pending. The run stays terminal ``eval_failed``.
    """

    def __init__(self, run_id: uuid.UUID, guardrail_name: str, detail: str) -> None:
        super().__init__(run_id, f"input still violates guardrail {guardrail_name!r}: {detail}")
        self.guardrail_name = guardrail_name
        self.detail = detail


class GuardrailOverrideNoCheckpointError(GuardrailOverrideError):
    """Raised when a node-started blocked run has no checkpoint to resume from.

    FAR-1711 (M1) fail-closed: a node-STARTED eval-blocked run can only be
    remediated by RESUMING it from its LangGraph checkpoint (FAR-1649: never
    re-execute a node-started run). When the run's thread carries no root
    checkpoint — the reachable case is a NON-interactive pipeline, where the
    executor attaches no checkpointer at all (FAR-432) and therefore never
    persists one — a resume cannot restore the completed supersteps. Routing
    such a run to ``resume_run`` would silently RE-EXECUTE it from the start
    (LangGraph seeds a fresh checkpoint and streams every node again —
    the exact FAR-1649/FAR-1711 sin), so the override is refused instead and
    the run stays terminal ``eval_failed``/``eval_blocked`` — visible as the
    block it is. The missing checkpoint never appears later (a
    ``first_node_dispatched`` thread with no root checkpoint stays that way),
    so re-calling the override returns the same 409: the true remediation is a
    NEW run against the pipeline, not another override attempt. Distinct from
    :class:`GuardrailOverrideRejectedError` (supplied input still violates a
    guardrail — 422): this is a mechanism/state refusal (409).
    """

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(
            run_id,
            "run started a node but has no resumable checkpoint — resuming would re-execute the "
            "node-started run (FAR-1649), so the override is refused; the run stays eval_blocked",
        )
        self.run_id = run_id


class GuardrailOverrideRequiredError(RuntimeError):
    """Raised when a guardrail-blocked run is routed through the generic recovery path.

    A guardrail-blocked run (terminal ``eval_failed`` / ``error_code``
    ``eval_blocked``) must NEVER be resurrected through :func:`recover_node`:
    the generic path does NOT re-run the guardrail pass on the supplied input,
    does not set ``is_replay=True``, and would resume execution on the blocked
    payload. The ONLY remediation is the guardrail-override endpoint
    (:func:`guardrail_override`), which re-runs the guardrail pass on the
    operator-supplied input (re-block safe default).
    """

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(
            f"Run {run_id} is blocked by a guardrail (eval_blocked) — the generic recovery path would not "
            "re-run the guardrail pass on the supplied input. Use the guardrail-override endpoint instead "
            "(it is re-block safe)."
        )
        self.run_id = run_id


_RECOVERABLE_STATUSES = frozenset({"failed", "awaiting_human"})

_RECOVERY_LOCK_STATUS = "running"

# SAQ job types for the two guardrail-override re-dispatch branches (FAR-1711).
_JOB_TYPE_RESUME = "resume_run"
_JOB_TYPE_EXECUTE = "execute_run"

# FAR-1711 (C1): the resume claim's stale-running arm matches ONLY when
# ``heartbeat_at < now() - run_claim_stale_seconds``. A run terminalised at
# block time keeps a FRESH heartbeat, so a bare ``status='running'`` flip is
# unclaimable for a whole stale window — the override returns 200, the SAQ
# ``resume_run`` job reads ``{"status": "not_claimed"}`` (success to SAQ, no
# retry), and the run is stranded until the age gate terminal-fails it. The
# flip therefore stamps the heartbeat back by one full claim-stale window.
# The extra skew guard covers clock drift in ONE direction: the stamp is
# computed on the APPLICATION clock while the claim compares it against the
# DATABASE's ``now()``, so the stamp must stay strictly older than
# ``now() - stale`` even when this process's clock LEADS (runs AHEAD of) the
# database — a lead makes the stamp more recent than intended, and the guard
# holds while the lead is under ``_CLAIM_STALE_SKEW_GUARD_SECONDS``
# (``app_now - db_now < 60``). A clock that LAGS the database only makes the
# stamp OLDER — the arm still matches, with extra margin — so lag is NOT the
# failure mode this guards. (A ``NULL`` heartbeat is NOT a fix — ``NULL < ...``
# is NULL, so the arm never matches.)
# SETTINGS INVARIANT (FAR-1728): the stamped depth is
# ``run_claim_stale_seconds + _CLAIM_STALE_SKEW_GUARD_SECONDS`` — 510 s at the
# defaults (450 + 60) — and ``slot_reconcile_stale_seconds`` (the
# ``run_admission.reconcile_pipeline_slots`` staleness window, default 1800)
# must stay STRICTLY GREATER than that depth. The override flips the run to
# ``running`` with this ALREADY-stale heartbeat, so a slot window at or below
# the depth would make the freshly-flipped row sweep-eligible immediately: for
# an OVER-BUDGET row (``claim_count > heartbeat_stale_retry_budget`` — the
# sweep's node-started exclusion guards the in-budget RETRY arm only) the slot
# sweep would terminal-fail it ``heartbeat_stale`` before ``resume_run`` ever
# claims it, destroying the override resume. Defaults are safe (1800 > 510);
# ``_immediately_claimable_heartbeat`` WARNs when a deployed config breaks the
# invariant, and this module owns the stamp — the invariant is documented here
# rather than as a Settings validator.
_CLAIM_STALE_SKEW_GUARD_SECONDS = 60

# Root-checkpoint existence probe (FAR-1711 M1). The root namespace
# (``checkpoint_ns = ''``) is the one the resume path's ``aupdate_state``
# reads for the run's ``langgraph_thread_id`` — the same predicate spelling
# the reconcile age gate's zero-progress shape uses against ``checkpoints``.
_HAS_ROOT_CHECKPOINT_SQL = text(
    "SELECT 1 FROM checkpoints WHERE organisation_id = :oid AND thread_id = :tid AND checkpoint_ns = '' LIMIT 1"
)


async def _run_has_resumable_checkpoint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    thread_id: str | None,
) -> bool:
    """True when the run's LangGraph thread carries a root checkpoint.

    The ``checkpoints`` table is the ModuloPostgresSaver runtime-DDL table
    (see ``core.pipeline_engine.modulo_saver``) — the same raw read the
    reconcile age gate and ``runs._get_checkpoint_state`` already perform.
    A run with no thread id can never have one.
    """
    if not thread_id:
        return False
    # Bind the org id as text: ``checkpoints.organisation_id`` is a UUID
    # column, but the SQLite-backed unit seam cannot bind a raw
    # ``uuid.UUID`` object, and Postgres accepts the text form identically.
    result = await session.execute(_HAS_ROOT_CHECKPOINT_SQL, {"oid": str(org_id), "tid": thread_id})
    return result.first() is not None


async def _resolve_override_node_started(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run: Run,
    run_id: uuid.UUID,
) -> bool:
    """Resolve the override's node-progress discriminator, enforcing FAR-1711 M1.

    Returns ``True`` for a node-STARTED run (the durable ``first_node_dispatched``
    phase a node-scoped ``block`` eval leaves behind when it terminalizes the run
    from a completed node) and ``False`` for the genuine zero-node block
    (run-level interception at creation, NULL / pre-node phase).

    FAR-1711 (M1): a node-STARTED block is remediable ONLY by resuming from
    checkpoint (FAR-1649 — never re-execute a node-started run), so a run whose
    thread carries no root checkpoint cannot be remediated by
    :func:`guardrail_override` at all. Fail CLOSED here — before the guardrail
    pass and before any state write — so the run stays terminal
    ``eval_failed``/``eval_blocked`` (re-calling the override returns the same
    409 — the checkpoint never appears, so the remediation is a NEW run)
    rather than being flipped and then silently re-executed by a
    ``resume_run`` that finds nothing to resume.
    """
    node_started = run.dispatch_phase == PHASE_FIRST_NODE_DISPATCHED
    if not node_started:
        return False
    if not await _run_has_resumable_checkpoint(session, org_id=org_id, thread_id=run.langgraph_thread_id):
        raise GuardrailOverrideNoCheckpointError(run_id)
    return True


def _immediately_claimable_heartbeat() -> datetime:
    """A heartbeat stamped one full claim-stale window (plus skew) in the past.

    Written into the SAME UPDATE that flips a node-started override run to
    ``running``, so :func:`modulo.core.pipeline_execution.claim_resume_run_async`'s
    stale-running arm matches on the very first claim attempt. The window is
    read from ``Settings.run_claim_stale_seconds`` (``RUN_CLAIM_STALE_SECONDS``
    — the exact bound the claim SQL binds), never hardcoded.
    """
    from modulo.settings import get_settings

    settings = get_settings()
    stale_seconds = int(settings.run_claim_stale_seconds)
    stamp_depth = stale_seconds + _CLAIM_STALE_SKEW_GUARD_SECONDS
    # FAR-1728 settings invariant (documented at
    # ``_CLAIM_STALE_SKEW_GUARD_SECONDS``): the slot sweep's window must stay
    # strictly above the stamp depth or the freshly-flipped run is swept
    # ``heartbeat_stale`` before ``resume_run`` claims it (over-budget
    # ``claim_count`` arm). WARN-only and isinstance-guarded (a test-double
    # Settings without the field skips the check); the shipped defaults
    # (1800 > 510) never fire it.
    slot_window = getattr(settings, "slot_reconcile_stale_seconds", None)
    if isinstance(slot_window, int) and 0 < slot_window <= stamp_depth:
        _log.warning(
            "guardrail_override.slot_sweep_window_below_heartbeat_stamp_depth "
            "(SLOT_RECONCILE_STALE_SECONDS=%s <= RUN_CLAIM_STALE_SECONDS + %s = %s): the override's "
            "stale-heartbeat stamp is already slot-sweep eligible, so the slot sweep can terminal-fail the "
            "flipped run before resume_run claims it — raise SLOT_RECONCILE_STALE_SECONDS above %s",
            slot_window,
            _CLAIM_STALE_SKEW_GUARD_SECONDS,
            stamp_depth,
            stamp_depth,
        )
    return datetime.now(UTC) - timedelta(seconds=stamp_depth)


# FAR-1728: the ONE durable discriminator for the guardrail-override resume
# self-heal leg in ``core.cron_helpers`` is the DEDICATED column
# ``runs.guardrail_override_resume_pending`` (migration 0305) — written by
# EXACTLY one site (:func:`guardrail_override`'s flip, in the same atomic
# UPDATE that flips the status) and read by exactly one leg (the reconcile's
# override-resume selection + its row-side recheck), and cleared by exactly
# one site (the resume claim, ``pipeline_execution._RESUME_CLAIM_UPDATE_SQL``
# family). Previously this was a sentinel string stuffed into
# ``runs.error_detail`` — a control-flow marker in an observation/error
# column that is exposed on REST/MCP responses; the dedicated boolean column
# replaces it (see migration 0305's docstring for the full rationale).
#
# ``runs.is_replay`` CANNOT be the discriminator, even though the flip also
# sets it: ``is_replay`` is written by webhook replay
# (``trigger_engine._create_webhook_run(..., is_replay=True)``) and by
# ``create_run`` for a manual replay, so a ``running`` +
# ``first_node_dispatched`` + ``is_replay`` row is NOT necessarily the
# guardrail-override flip. Selecting the leg on ``is_replay`` lifts the
# FAR-1649 node-started belt for EVERY replay-shaped row — a wedged
# webhook-replay run would be auto-resumed from checkpoint, re-executing an
# in-progress, possibly non-idempotent super-step (the exact hazard the belt
# guards). The dedicated column has a single write site, so
# ``git grep guardrail_override_resume_pending`` IS the uniqueness proof.


def _override_flip_values(node_started: bool) -> tuple[str, dict[str, Any], datetime | None]:
    """Status UPDATE values for a guardrail override (FAR-1711).

    A node-STARTED run flips to ``running`` (the recover-node lock status whose
    stale-running arm the resume claim matches), carries a ``heartbeat_at``
    stamped one claim-stale window into the past so the resume claim is
    IMMEDIATELY claimable, and — FAR-1728 — sets the dedicated
    ``guardrail_override_resume_pending`` marker IN THE SAME atomic UPDATE (so
    the self-heal discriminator can never exist without the flip, or vice
    versa). The genuine zero-node block flips to ``pending`` with no heartbeat
    stamp and does NOT set the marker: nothing is resumable there (the run is
    re-dispatched ``execute_run`` and recovered by the pending-class legs), and
    a "resume pending" flag would linger forever. Returns
    ``(new_status, update_values, stale_heartbeat)``.
    """
    if not node_started:
        return "pending", {"status": "pending"}, None
    stale_heartbeat = _immediately_claimable_heartbeat()
    return (
        _RECOVERY_LOCK_STATUS,
        {
            "status": _RECOVERY_LOCK_STATUS,
            "heartbeat_at": stale_heartbeat,
            "guardrail_override_resume_pending": True,
        },
        stale_heartbeat,
    )


def guardrail_override_job_type(run: Run) -> str:
    """SAQ job type for re-dispatching a run :func:`guardrail_override` flipped.

    FAR-1711: the job type is chosen by the SAME ``_RECOVERY_LOCK_STATUS``
    flip target the override applied — a node-started run left ``running``
    MUST be re-dispatched ``resume_run`` (checkpoint resume, FAR-1649: never
    re-execute a node-started run); the genuine zero-node block left
    ``pending`` is re-dispatched ``execute_run`` from run start. Exported as
    the one shared predicate so the API route never re-derives the decision
    from a magic status string owned by this module.
    """
    return _JOB_TYPE_RESUME if run.status == _RECOVERY_LOCK_STATUS else _JOB_TYPE_EXECUTE


async def recover_node(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    input_data: dict[str, Any] | None,
    actor_id: uuid.UUID | None = None,
) -> Run:
    """Validate and prepare a manual-input node for recovery.

    Two modes:
      * **Re-run** — ``input_data`` is a dict of new manual output for the node.
      * **Skip** — ``input_data`` is ``None``; the node is marked completed with
        a null output and the run proceeds.

    Returns the updated ``Run`` row.  The caller must then resume the run via
    ``PipelineExecutor.resume()`` with the returned ``resume_data`` dict.

    Returns:
        The updated Run row.

    Raises:
        RecoveryNotAllowedError — run is not in a recoverable state.
        GuardrailOverrideRequiredError — run is guardrail-blocked (eval_blocked)
            and must be remediated via the guardrail-override endpoint instead.
        NodeNotFoundInGraphError — node_id does not exist in the graph.
        NodeAlreadyCompletedError — node has already been completed.
        ConcurrentRecoveryError — another recovery won the race.

    """
    run = await _fetch_locked_run(session, run_id)
    _require_recoverable_status(run, run_id)
    node_type = await _resolve_node_type(session, run, run_id, node_id)
    blobs = await read_run_blobs(session, run_id=run_id, organisation_id=run.organisation_id)
    _require_node_not_completed(run, run_id, node_id, blobs)
    await _acquire_recovery_lock(session, run, run_id)

    # FAR-583: the marker write is a store-write chokepoint. A DualWriteError is
    # caught here (the catch/orchestrate contract): the caller's transaction is
    # rolled back cleanly, the run is terminalized ``dual_write_failed`` by the
    # separate-session orchestrator, and the error re-raises so the recovery
    # attempt fails closed instead of resuming a run whose new-table evidence
    # is missing.
    from modulo.core.run_outputs_dualwrite import guard_dual_write

    async with guard_dual_write(session):
        await _apply_recovery_markers(session, run, node_id, input_data)
        await session.flush()
    await _record_recovery_audit(
        session,
        org_id,
        run_id,
        node_id,
        node_type,
        input_data,
        actor_id,
        # FAR-1141 / ADR-042: the helper derives provenance from the run row
        # this function already holds, so the audit adds no extra read.
        run=run,
    )

    return run


async def _fetch_locked_run(session: AsyncSession, run_id: uuid.UUID) -> Run:
    """Load the run, take the pipeline row lock, and re-fetch the latest run.

    Serialises recovery attempts for runs on the same pipeline. Raises
    :class:`RecoveryNotAllowedError` if the run disappears under the lock.
    """
    run = await get_run(session, run_id)
    if run is None:
        raise RecoveryNotAllowedError(run_id, "not_found")

    # FAR-1025: opt out of the global soft-delete filter -- an in-flight
    # run's pipeline may have been soft-deleted; the pipeline lock must still
    # be acquired for recovery.
    from modulo.db.soft_delete import include_soft_deleted

    await session.execute(
        include_soft_deleted(select(Pipeline).where(Pipeline.id == run.pipeline_id).with_for_update())
    )

    # Re-fetch the run after the lock to get the latest status.
    run = await get_run(session, run_id)
    if run is None:
        raise RecoveryNotAllowedError(run_id, "not_found")

    return run


def _require_recoverable_status(run: Run, run_id: uuid.UUID) -> None:
    """Raise unless the run is in a generic-recoverable status.

    A guardrail-blocked run (terminal ``eval_failed`` with ``error_code``
    ``eval_blocked``) must never be resurrected through the generic recovery
    path — it does not re-run the guardrail pass and would resume execution on
    the blocked payload. Point the caller at the re-block-safe override
    endpoint instead.
    """
    if run.status in _RECOVERABLE_STATUSES:
        return

    if run.status == "eval_failed" and run.error_code == "eval_blocked":
        raise GuardrailOverrideRequiredError(run_id)

    raise RecoveryNotAllowedError(run_id, run.status)


async def _resolve_node_type(session: AsyncSession, run: Run, run_id: uuid.UUID, node_id: str) -> Any:
    """Resolve the node definition from the run's snapshot graph.

    Raises :class:`NodeNotFoundInGraphError` if the node is absent from the
    graph. Returns the node's ``node_type`` (defaulting to ``"agent"``).
    """
    snapshot_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
    snapshot = snapshot_result.scalar_one_or_none()
    if snapshot is None:
        raise RuntimeError(f"Snapshot {run.snapshot_id} not found for run {run_id}")

    graph_json: dict[str, Any] = snapshot.graph_json
    nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
    node_def = next((n for n in nodes if str(n.get("id")) == node_id), None)
    if node_def is None:
        raise NodeNotFoundInGraphError(run_id, node_id)

    return node_def.get("node_type", "agent")


def _require_node_not_completed(_run: Run, run_id: uuid.UUID, node_id: str, blobs: Any) -> None:
    """Raise if the node already has a completed marker in either blob side.

    The node may hold a PURE return on its *outputs* side (a replay marker)
    or ONLY a telemetry record (a skip marker omits the outputs key — Agent
    Return Contract, FAR-125 P1c), so both sides must be checked. FAR-583 B1:
    the check reads through the repo's blobs reader (new-table-only since B2c,
    legacy fallback removed) instead of the cut ORM mapping — post-B1
    completions live ONLY
    in ``run_node_outputs``, so a legacy-only read would silently let a
    recovered node replay twice.
    """
    outputs = dict(blobs.outputs) if blobs.outputs else {}
    telemetry = dict(blobs.telemetry) if blobs.telemetry else {}
    if node_id in outputs or node_id in telemetry:
        raise NodeAlreadyCompletedError(run_id, node_id)


async def _acquire_recovery_lock(session: AsyncSession, run: Run, run_id: uuid.UUID) -> None:
    """Flip the run to the lock status via an optimistic WHERE clause.

    Serialises the status update with optimistic locking; a concurrent recovery
    attempt loses the WHERE match and raises :class:`ConcurrentRecoveryError`.
    Updates ``run.status`` in place on success.
    """
    stmt = (
        update(Run)
        .where(
            Run.id == run_id,
            Run.status.in_(_RECOVERABLE_STATUSES),
        )
        .values(status=_RECOVERY_LOCK_STATUS)
        .returning(Run.id)
    )
    locked_result = await session.execute(stmt)
    locked_id = locked_result.scalar_one_or_none()
    if locked_id is None:
        raise ConcurrentRecoveryError(run_id)

    run.status = _RECOVERY_LOCK_STATUS


async def _apply_recovery_markers(
    session: AsyncSession, run: Run, node_id: str, input_data: dict[str, Any] | None
) -> None:
    """Write the recovery markers onto the run's blob sides.

    The PURE return lands on the *outputs* side and the marker on the
    *telemetry* side; a skipped node OMITS its outputs key entirely (the
    telemetry entry is the sole record). FAR-583 B1: the legacy ``runs`` blob
    columns are no longer written (their ORM mapping is cut) — the merged
    dicts are persisted on ``run_node_outputs`` by the primary repo write
    (:func:`modulo.db.crud.run.write_run_outputs_from_run`), same-transaction.
    Requires an active transaction on *session* (the caller's); a
    :class:`DualWriteError` propagates to :func:`recover_node`'s guard for
    the catch/orchestrate contract.

    The PRE-mutation dicts (qa Major 4) are captured and passed as
    *inherited_outputs* / *inherited_telemetry* (mirroring the branches in
    ``crud.run``) — pre-0176 legacy data carrying ``__``-prefixed sentinel
    keys (inherited junk) would otherwise make the REPLACE raise
    :class:`OutputsSentinelViolation` and wedge the node's recovery
    permanently (the exact wedge the M19 fix removed from the other
    chokepoints).
    """
    from modulo.db.crud.run import write_run_outputs_from_run
    from modulo.db.crud.run_node_outputs import read_run_blobs

    stored = await read_run_blobs(session, run_id=run.id, organisation_id=run.organisation_id)
    outputs: dict[str, Any] = dict(stored.outputs) if stored.outputs else {}
    telemetry: dict[str, Any] = dict(stored.telemetry) if stored.telemetry else {}

    # qa Major 4: capture PRE-mutation state for the inherited-sentinel
    # filter (the store write compares against exactly these dicts).
    inherited_outputs = dict(outputs)
    inherited_telemetry = dict(telemetry)

    if input_data is not None:
        outputs[node_id] = input_data
        telemetry[node_id] = {"recovered": True, "recovery_input": input_data}
    else:
        telemetry[node_id] = {"skipped": True}

    await write_run_outputs_from_run(
        session,
        run_id=run.id,
        organisation_id=run.organisation_id,
        outputs=outputs,
        telemetry=telemetry,
        claim_token=run.claim_token,
        origin="recovery.apply_recovery_markers",
        inherited_outputs=inherited_outputs,
        inherited_telemetry=inherited_telemetry,
    )


async def _record_recovery_audit(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    node_type: str,
    input_data: dict[str, Any] | None,
    actor_id: uuid.UUID | None,
    run: Run,
) -> None:
    """Emit the ``node.recovery`` audit event and the applied-log line.

    Audit recording failures are non-fatal — they are logged and swallowed so a
    recovery is never blocked by a transient audit write error.

    ``run`` (FAR-1141 / ADR-042, composed via FAR-1565's shared serializer) is
    the row the caller already holds, so this helper never adds a read; the
    provenance key is ALWAYS present in the payload ('dispatched' / NULL),
    never omitted.
    """
    action = "skip" if input_data is None else "replay"
    payload: dict[str, Any] = {
        "node_id": node_id,
        "node_type": node_type,
        "recovery_action": action,
        # FAR-1141 / ADR-042: run-keyed audit payloads carry the run's
        # execution origin so a recovery on a dispatched run is distinguishable
        # from one on a run Modulo executed itself — from the ONE serializer.
        **run_provenance_fields(run),
        "summary": f'{action.capitalize()} recovery applied to node "{node_id}"',
    }
    if actor_id is None:
        payload["actor"] = SYSTEM_ACTOR
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="node.recovery",
            actor_user_id=actor_id,
            resource_type="run",
            resource_id=run_id,
            payload_json=payload,
        )
    except Exception:
        _log.exception("Failed to record recovery audit event for run %s", run_id)

    _log.info(
        "node.recovery.applied",
        extra={
            "run_id": str(run_id),
            "node_id": node_id,
            "action": action,
        },
    )


async def guardrail_override(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    input_data: dict[str, Any],
    actor_id: uuid.UUID | None = None,
) -> Run:
    """Remediate a guardrail-blocked run with operator-supplied input.

    Guardrail remediation is the DEDICATED counterpart of :func:`recover_node`
    (FAR-208 item 6). :func:`recover_node` refuses guardrail-blocked runs
    (:class:`GuardrailOverrideRequiredError`); a guardrail block is TERMINAL
    ``eval_failed`` with NO HITL gate (deliver_manual is 404 on such runs — see
    the guardrails spike tests), so the ONLY remediation is this override:

    1. Pipeline ``FOR UPDATE`` lock + optimistic status UPDATE (single-flight
       by run_id) — mirrors ``recover_node``.
    2. The override RE-RUNS the guardrail pass on the operator-supplied input
       (re-block safe default): a still-violating input raises
       :class:`GuardrailOverrideRejectedError` and the run STAYS terminal.
    3. On clean input the run flips off ``eval_failed`` with the POST-redaction
       payload persisted, ``is_replay=True`` (so lifecycle-map
       journeys increment EXACTLY ONCE on the re-dispatch), the dedicated
       ``guardrail_override_resume_pending`` marker (FAR-1728: the reconcile
       self-heal leg's discriminator — see the column's note on the model and
       migration 0305 for why ``is_replay`` cannot serve and why the marker is
       no longer a sentinel in ``error_detail``), and a
       ``guardrail.override`` audit event. The flip target branches on node
       progress (FAR-1711): a node-STARTED run — the durable
       ``first_node_dispatched`` phase (FAR-1422) that a node-scoped ``block``
       eval leaves behind when it terminalizes the run from a completed node —
       flips to ``running`` (the recover-node lock status, whose stale-running
       arm the resume claim matches) WITH its ``heartbeat_at`` stamped one
       claim-stale window into the past, so the resume claim is IMMEDIATELY
       claimable, and WITH the ``guardrail_override_resume_pending`` marker set
       (in the same atomic UPDATE); the genuine zero-node block (run-level
       interception at creation, NULL / pre-node phase) flips to ``pending``
       and does NOT set the marker (nothing to resume there).

    The caller re-dispatches accordingly (``guardrail_override_job_type``):
    ``resume_run`` for a node-started run (checkpoint resume — FAR-1649: a
    node-started run may only be resumed from checkpoint, never re-executed),
    ``execute_run`` from run start for the zero-node case (no node ever ran,
    so there is nothing to resume). ``is_replay=True`` makes the re-dispatch
    detection-only.

    A node-started run is RESUMABLE ONLY when its thread carries a root
    checkpoint (FAR-1711 M1): a NON-interactive pipeline attaches no
    checkpointer at all (FAR-432), so its node-started block has no
    checkpoint to resume from, and a resume would silently re-execute the
    run from the start. Such a run is refused with
    :class:`GuardrailOverrideNoCheckpointError` BEFORE any state is touched —
    it stays terminal ``eval_failed``/``eval_blocked`` rather than being
    burned into an unrecoverable re-execution. The missing checkpoint never
    appears later, so re-calling the override returns the same 409: the true
    remediation is a NEW run against the pipeline, not another override
    attempt.

    Raises:
        GuardrailOverrideError — run is not a guardrail-blocked terminal run,
            or it was blocked by a policy-gate pin fingerprint mismatch
            (FAR-967 F1: a mechanism failure no input can remediate).
        GuardrailOverrideRejectedError — supplied input still violates a guardrail,
            or the re-run pass fail-closed on a destroy-redaction mechanism error
            (FAR-209 Track B: unset/unusable pepper).
        GuardrailOverrideNoCheckpointError — a node-started run with no
            resumable checkpoint (FAR-1711 M1); the run stays terminal.
        ConcurrentRecoveryError — another override won the race.
    """
    run = await get_run(session, run_id)
    if run is None:
        raise GuardrailOverrideError(run_id, "run not found")

    # FAR-1025: opt out of the global soft-delete filter -- an in-flight
    # run's pipeline may have been soft-deleted; the pipeline lock must still
    # be acquired for guardrail override.
    from modulo.db.soft_delete import include_soft_deleted

    await session.execute(
        include_soft_deleted(select(Pipeline).where(Pipeline.id == run.pipeline_id).with_for_update())
    )

    run = await get_run(session, run_id)
    if run is None:
        raise GuardrailOverrideError(run_id, "run not found")
    if run.status != "eval_failed" or run.error_code != "eval_blocked":
        raise GuardrailOverrideError(
            run_id, f"status={run.status!r} error_code={run.error_code!r} (expected eval_failed/eval_blocked)"
        )

    # FAR-967 F1: a pin-fingerprint mismatch is a MECHANISM failure (the
    # snapshot's pinned gate set and its stored digest disagree), not a
    # payload violation — no operator-supplied input can remediate it, so
    # the re-run pass below would be meaningless. Refuse BEFORE touching the
    # guardrail rows; the run stays terminal and the only remediation is a
    # new run against a fresh snapshot. Matched by marker so a genuinely
    # guardrail-blocked run (whose error_detail carries the guardrail block
    # message) is unaffected.
    if run.error_detail and POLICY_GATE_PIN_MISMATCH_MARKER in run.error_detail:
        raise GuardrailOverrideError(
            run_id,
            f"{POLICY_GATE_PIN_MISMATCH_MARKER} — not overridable; create a new run",
        )

    # FAR-1711 (M1): resolve the node-progress discriminator AND enforce its
    # resumable-checkpoint precondition (fail CLOSED) here — before the
    # guardrail pass and before any state write — so a node-started block with
    # no checkpoint to resume from is refused while it is still terminal rather
    # than flipped and silently re-executed. Same precondition grouping as the
    # pin-mismatch refusal above: a mechanism/state failure no input can
    # remediate. See :func:`_resolve_override_node_started`.
    node_started = await _resolve_override_node_started(session, org_id=org_id, run=run, run_id=run_id)

    # Re-run the guardrail pass on the operator-supplied input BEFORE flipping
    # the run (re-block safe default). Persisted state is post-redaction.
    # The bounded async pass applies the same per-guardrail hard timeout +
    # bounded-payload budget as the run-creation seam (FAR-223 item 7).
    from modulo.core.eval_engine import EvalEngine
    from modulo.core.guardrails import (
        GuardrailInterceptionOutcome,
        GuardrailRedactionMechanismError,
        run_interception_pass_async,
        to_engine_definition,
    )
    from modulo.db.crud.guardrail_config import load_pipeline_guardrail_rows

    guardrail_rows = await load_pipeline_guardrail_rows(
        session,
        pipeline_id=run.pipeline_id,
        organisation_id=org_id,
    )
    guardrail_defs = [to_engine_definition(row) for row in guardrail_rows]
    try:
        outcome: GuardrailInterceptionOutcome = await run_interception_pass_async(
            EvalEngine(),
            guardrail_defs,
            input_data,
            detection_only=False,
        )
    except GuardrailRedactionMechanismError as exc:
        # FAR-209 Track B: a destroy-mode mechanism failure (pepper unset or
        # unusable) escapes the pass (see ``_apply_redaction_phase``). The
        # override is re-block-safe, so fail CLOSED — reject the override and
        # leave the run terminal, exactly like a still-violating input, rather
        # than letting the mechanism error surface as an unhandled 500.
        raise GuardrailOverrideRejectedError(
            run_id,
            "<guardrail>",
            f"guardrail mechanism error: {exc}",
        ) from exc
    if outcome.blocked:
        raise GuardrailOverrideRejectedError(
            run_id,
            outcome.blocking_eval_name or "<guardrail>",
            outcome.block_message,
        )

    # FAR-1711: node-progress gate. A node-SCOPED ``block`` eval terminalizes
    # the run from a COMPLETED node, so the "blocked run never executed" premise
    # is FALSE for that class: such a row carries the durable
    # ``first_node_dispatched`` phase (FAR-1422). FAR-1649: a node-started run
    # may only be resumed from checkpoint, never re-executed, so the flip target
    # branches — ``running`` (the recover-node lock status the resume claim
    # matches, with its heartbeat stamped stale so the claim is immediately
    # claimable — FAR-1711 C1) for a node-started run, ``pending`` for the
    # genuine zero-node block (re-dispatched ``execute_run`` from run start).
    # ``PHASE_FIRST_NODE_DISPATCHED`` is the ONE shared discriminator every
    # reconcile re-dispatch decision binds — never a bare literal.
    # ``node_started`` was resolved (and its resumable-checkpoint precondition
    # enforced) BEFORE the guardrail pass above.
    new_status, flip_values, stale_heartbeat = _override_flip_values(node_started)

    # Optimistic status UPDATE — a concurrent override loses the WHERE match.
    stmt = (
        update(Run)
        .where(
            Run.id == run_id,
            Run.status == "eval_failed",
            Run.error_code == "eval_blocked",
        )
        .values(**flip_values)
        .returning(Run.id)
    )
    locked_result = await session.execute(stmt)
    locked_id = locked_result.scalar_one_or_none()
    if locked_id is None:
        raise ConcurrentRecoveryError(run_id)

    run.status = new_status
    if stale_heartbeat is not None:
        # Keep the in-session row consistent with the atomic UPDATE above.
        run.heartbeat_at = stale_heartbeat
        # FAR-1728: the self-heal leg's discriminator, set IN the atomic
        # UPDATE above (``_override_flip_values``) — mirrored on the in-session
        # row so it is consistent for this request. ``error_detail`` is
        # DELIBERATELY left untouched: it is an observation column exposed on
        # REST/MCP responses and must never carry a control-flow marker (the
        # pre-0305 sentinel-string design is retired). ``error_code`` stays
        # ``None`` so no failure surface renders anything. A lost
        # ``resume_run`` job is then re-dispatched by the reconcile leg that
        # selects on this column — and by that leg alone: ``is_replay`` (also
        # set below) is shared with webhook/manual replay, so it can never
        # discriminate the flip.
        run.guardrail_override_resume_pending = True
    run.error_code = None
    run.input_payload = outcome.payload
    # The stored payload changed on override, so the persisted ``input_hash``
    # (computed from the original blocked payload at create) must be recomputed
    # from the post-redaction payload actually stored — the stored hash must
    # always match the stored payload. The block-time ``completed_at`` stamp is
    # cleared because the run is no longer terminal.
    run.input_hash = _input_hash(run.input_payload)
    run.completed_at = None
    run.is_replay = True
    await session.flush()

    override_payload: dict[str, Any] = {
        "node_id": None,
        "action": "override",
        "is_replay": True,
        "summary": "Guardrail override applied (run requeued as replay)",
    }
    if actor_id is None:
        override_payload["actor"] = SYSTEM_ACTOR
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="guardrail.override",
            actor_user_id=actor_id,
            resource_type="run",
            resource_id=run_id,
            payload_json=override_payload,
        )
    except Exception:
        _log.exception("Failed to record guardrail override audit event for run %s", run_id)

    _log.info(
        "guardrail.override.applied",
        extra={
            "run_id": str(run_id),
            "action": "override",
            "is_replay": True,
        },
    )

    return run
