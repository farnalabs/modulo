"""Recovery handler for failed manual-input nodes.

Provides the core logic to replay or skip a manual node that failed or
is awaiting human input.  Used by the ``POST /recover`` API endpoint.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.run import get_run
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

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
        NodeNotFoundInGraphError — node_id does not exist in the graph.
        NodeAlreadyCompletedError — node has already been completed.
        ConcurrentRecoveryError — another recovery won the race.
    """
    await set_rls_org(session, org_id)

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
        raise RecoveryNotAllowedError(run_id, run.status)

    snapshot_result = await session.execute(
        select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id)
    )
    snapshot = snapshot_result.scalar_one_or_none()
    if snapshot is None:
        raise RuntimeError(f"Snapshot {run.snapshot_id} not found for run {run_id}")

    graph_json: dict[str, Any] = snapshot.graph_json
    nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
    node_def = next((n for n in nodes if str(n.get("id")) == node_id), None)
    if node_def is None:
        raise NodeNotFoundInGraphError(run_id, node_id)

    node_type = node_def.get("node_type", "agent")

    # Check for already-completed node in outputs.
    outputs = dict(run.outputs_json) if run.outputs_json else {}
    if node_id in outputs:
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

    # Store recovery output in outputs_json.
    if input_data is not None:
        outputs[node_id] = {
            "input": input_data,
            "output": input_data,
            "recovered": True,
        }
    else:
        outputs[node_id] = {
            "input": None,
            "output": None,
            "skipped": True,
        }

    run.outputs_json = outputs
    await session.flush()

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

    _log.info(
        "node.recovery.applied",
        extra={
            "run_id": str(run_id),
            "node_id": node_id,
            "action": "skip" if input_data is None else "replay",
        },
    )

    return run
