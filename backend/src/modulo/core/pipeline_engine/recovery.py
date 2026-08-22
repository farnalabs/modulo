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
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.run import _input_hash, get_run
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
    # Serialise on the pipeline row to prevent concurrent recovery attempts
    # for runs on the same pipeline.
    run = await get_run(session, run_id)
    if run is None:
        raise RecoveryNotAllowedError(run_id, "not_found")

    await session.execute(select(Pipeline).where(Pipeline.id == run.pipeline_id).with_for_update())

    # Re-fetch the run after the lock to get the latest status.
    run = await get_run(session, run_id)
    if run is None:
        raise RecoveryNotAllowedError(run_id, "not_found")

    if run.status not in _RECOVERABLE_STATUSES:
        if run.status == "eval_failed" and run.error_code == "eval_blocked":
            # A guardrail-blocked run must never be resurrected through the
            # generic recovery path (no guardrail re-pass, no is_replay=True —
            # the blocked payload would flow into the pipeline anyway). Point
            # the caller at the re-block-safe override endpoint.
            raise GuardrailOverrideRequiredError(run_id)
        raise RecoveryNotAllowedError(run_id, run.status)

    snapshot_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
    snapshot = snapshot_result.scalar_one_or_none()
    if snapshot is None:
        raise RuntimeError(f"Snapshot {run.snapshot_id} not found for run {run_id}")

    graph_json: dict[str, Any] = snapshot.graph_json
    nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
    node_def = next((n for n in nodes if str(n.get("id")) == node_id), None)
    if node_def is None:
        raise NodeNotFoundInGraphError(run_id, node_id)

    node_type = node_def.get("node_type", "agent")

    # Check for already-completed node in either output column. A skipped
    # recovery marker lives ONLY in node_telemetry_json (its outputs key is
    # omitted), so the guard must check both columns (Agent Return Contract,
    # FAR-125 P1c).
    outputs = dict(run.outputs_json) if run.outputs_json else {}
    telemetry = dict(run.node_telemetry_json) if run.node_telemetry_json else {}
    if node_id in outputs or node_id in telemetry:
        raise NodeAlreadyCompletedError(run_id, node_id)

    # Serialise status update with optimistic locking via the WHERE clause.
    new_status = "running"
    stmt = (
        update(Run)
        .where(
            Run.id == run_id,
            Run.status.in_(_RECOVERABLE_STATUSES),
        )
        .values(status=new_status)
        .returning(Run.id)
    )
    locked_result = await session.execute(stmt)
    locked_id = locked_result.scalar_one_or_none()
    if locked_id is None:
        raise ConcurrentRecoveryError(run_id)

    run.status = new_status

    # Store recovery markers in the split output columns (Agent Return
    # Contract, FAR-125). The PURE return lands in outputs_json and the marker
    # in node_telemetry_json; a skipped node OMITS its outputs key entirely
    # (the telemetry entry is the sole record). Both columns are written on the
    # same ORM object and flushed once so the pair lands atomically — the same
    # pattern as update_run_outputs / update_run_status.
    if input_data is not None:
        outputs[node_id] = input_data
        telemetry[node_id] = {"recovered": True, "recovery_input": input_data}
    else:
        telemetry[node_id] = {"skipped": True}

    run.outputs_json = outputs
    run.node_telemetry_json = telemetry
    await session.flush()

    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="node.recovery",
            actor_user_id=actor_id,
            resource_type="run",
            resource_id=run_id,
            payload_json={
                "node_id": node_id,
                "node_type": node_type,
                "recovery_action": "skip" if input_data is None else "replay",
            },
        )
    except Exception:
        _log.exception("Failed to record recovery audit event for run %s", run_id)

    _log.info(
        "node.recovery.applied",
        extra={
            "run_id": str(run_id),
            "node_id": node_id,
            "action": "skip" if input_data is None else "replay",
        },
    )

    return run


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
    3. On clean input the run flips ``eval_failed -> pending`` with the
       POST-redaction payload persisted, ``is_replay=True`` (so lifecycle-map
       journeys increment EXACTLY ONCE on the re-dispatch), and a
       ``guardrail.override`` audit event.

    The caller re-dispatches the pending run with ``execute_run`` (from run
    start — the blocked run never executed, so there is no checkpoint to
    resume). ``is_replay=True`` makes the re-dispatch detection-only.

    Raises:
        GuardrailOverrideError — run is not a guardrail-blocked terminal run.
        GuardrailOverrideRejectedError — supplied input still violates a guardrail.
        ConcurrentRecoveryError — another override won the race.
    """
    run = await get_run(session, run_id)
    if run is None:
        raise GuardrailOverrideError(run_id, "run not found")

    await session.execute(select(Pipeline).where(Pipeline.id == run.pipeline_id).with_for_update())

    run = await get_run(session, run_id)
    if run is None:
        raise GuardrailOverrideError(run_id, "run not found")
    if run.status != "eval_failed" or run.error_code != "eval_blocked":
        raise GuardrailOverrideError(
            run_id, f"status={run.status!r} error_code={run.error_code!r} (expected eval_failed/eval_blocked)"
        )

    # Re-run the guardrail pass on the operator-supplied input BEFORE flipping
    # the run (re-block safe default). Persisted state is post-redaction.
    # The bounded async pass applies the same per-guardrail hard timeout +
    # bounded-payload budget as the run-creation seam (FAR-223 item 7).
    from modulo.core.eval_engine import EvalEngine
    from modulo.core.guardrails import (
        GuardrailInterceptionOutcome,
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
    outcome: GuardrailInterceptionOutcome = await run_interception_pass_async(
        EvalEngine(),
        guardrail_defs,
        input_data,
        detection_only=False,
    )
    if outcome.blocked:
        raise GuardrailOverrideRejectedError(
            run_id,
            outcome.blocking_eval_name or "<guardrail>",
            outcome.block_message,
        )

    # Optimistic status UPDATE — a concurrent override loses the WHERE match.
    new_status = "pending"
    stmt = (
        update(Run)
        .where(
            Run.id == run_id,
            Run.status == "eval_failed",
            Run.error_code == "eval_blocked",
        )
        .values(status=new_status)
        .returning(Run.id)
    )
    locked_result = await session.execute(stmt)
    locked_id = locked_result.scalar_one_or_none()
    if locked_id is None:
        raise ConcurrentRecoveryError(run_id)

    run.status = new_status
    run.error_code = None
    run.error_detail = None
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

    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="guardrail.override",
            actor_user_id=actor_id,
            resource_type="run",
            resource_id=run_id,
            payload_json={
                "node_id": None,
                "action": "override",
                "is_replay": True,
            },
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
