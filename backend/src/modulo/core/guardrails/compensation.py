"""Run-termination compensation for guardrail-blocked runs (FAR-213).

When a run terminalizes ``eval_failed``/``eval_blocked`` from a guardrail block
(T1 terminal-block semantics), any external side effects its executed nodes
already performed (a pushed PR, a flipped Linear status, a sent notification)
stand unless compensated. This module orchestrates per-node connector
compensating callbacks (best-effort + failure-isolated), writes the
``blocked_partial`` run summary (executed nodes, per-node publish status,
output references — never duplicated raw payloads), and records audit events.

Wiring: :func:`compensate_blocked_run` is invoked from the guardrail-blocked
terminalization seam in ``db.crud.run.create_run`` AFTER the terminal status
write, and is the general entry point for any caller that terminalizes a run
with executed node outputs and a connector hub. It NEVER raises into the
terminalization path — every step is best-effort with a log + audit.

Connector support: connectors OPT IN by overriding
:meth:`~modulo.connectors.base.ConnectorBase.compensate` (GitHub closes a PR,
Linear unassigns/archives). The default returns ``not_supported``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.connectors.base import (
    CompensationContext,
    CompensationOperation,
    CompensationOutcome,
    CompensationResult,
)
from modulo.core.audit_logger import append_audit_event
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

# Audit event names (free-form — there is no central event-type enumeration).
EVENT_COMPENSATION_ATTEMPTED = "guardrail.compensation_attempted"
EVENT_COMPENSATION_FAILED = "guardrail.compensation_failed"
EVENT_BLOCKED_PARTIAL_WRITTEN = "guardrail.blocked_partial_written"

# Summary-only caps — audit/log payloads never carry raw node output.
_SUMMARY_MESSAGE_CAP = 2000
_SUMMARY_REASON_CAP = 500


def _parse_uuid(value: Any) -> uuid.UUID | None:
    """Parse a connector instance id from a graph binding, or ``None``."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _normalise_executed_nodes(run: Run, executed_nodes: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the executed-node map, keeping only dict outputs.

    Falls back to the run's persisted ``outputs_json`` when the caller does not
    supply one. ``outputs_json`` is keyed by node_id in completion order, so the
    summary's executed-node order is deterministic.
    """
    raw = dict(run.outputs_json or {}) if executed_nodes is None else dict(executed_nodes)
    return {str(node_id): value for node_id, value in raw.items() if isinstance(value, dict)}


async def _load_graph_nodes(session: AsyncSession, run: Run) -> list[dict[str, Any]]:
    """Load the snapshot's graph nodes (best-effort; never raises)."""
    try:
        result = await session.execute(
            select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == run.snapshot_id)
        )
        graph = result.scalar_one_or_none()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.compensation.graph_load_failed run=%s", run.id)
        return []
    if not isinstance(graph, dict):
        return []
    nodes = graph.get("nodes")
    return nodes if isinstance(nodes, list) else []


async def _append_attempt_audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    resource: str,
    outcome: CompensationOutcome,
    detail: str,
) -> None:
    """Record a compensation attempt audit event (best-effort, guard-the-guard).

    Summary-only payload — node id, resource, outcome, truncated reason. Never
    raw payloads.
    """
    payload = {
        "node_id": node_id,
        "resource": resource,
        "outcome": outcome.value,
        "reason": (detail or "")[:_SUMMARY_REASON_CAP],
    }
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=EVENT_COMPENSATION_ATTEMPTED,
            resource_type="run",
            resource_id=run_id,
            payload_json=payload,
        )
        if outcome == CompensationOutcome.FAILED:
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=EVENT_COMPENSATION_FAILED,
                resource_type="run",
                resource_id=run_id,
                payload_json={
                    "node_id": node_id,
                    "resource": resource,
                    "reason": (detail or "")[:_SUMMARY_REASON_CAP],
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.compensation.audit_failed run=%s node=%s", run_id, node_id)


async def _append_summary_audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    blocking_eval_name: str,
    executed: dict[str, Any],
) -> None:
    """Record the blocked_partial summary write (best-effort, guard-the-guard)."""
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=EVENT_BLOCKED_PARTIAL_WRITTEN,
            resource_type="run",
            resource_id=run_id,
            payload_json={
                "blocking_eval_name": blocking_eval_name or None,
                "executed_node_count": len(executed),
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.compensation.summary_audit_failed run=%s", run_id)


async def _compensate_one_node(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    node_output: dict[str, Any],
    node_def: dict[str, Any],
    connector_hub: Any,
    guardrail_block: str,
) -> dict[str, Any]:
    """Compensate one executed node's connector side effect (failure-isolated).

    Returns a per-node summary entry. One node's failure never prevents the
    others and never raises into the terminalization path.
    """
    binding = node_def.get("connector_binding") or {}
    instance_id = _parse_uuid(binding.get("instance_id"))
    resource = binding.get("resource")
    if instance_id is None or not isinstance(resource, str) or not resource:
        # Not a connector write node (agent/sandbox/manual), or the graph has
        # no binding for it — Modulo has no inverse for an agent's own external
        # side effects; the side effect stands ("published").
        return {
            "node_id": node_id,
            "publish_status": "published",
            "output_ref": {"run_id": str(run_id), "node_id": node_id},
            "compensation": None,
        }

    data = binding.get("data", {})
    raw_output = node_output.get("output") if isinstance(node_output, dict) else None
    operation = CompensationOperation(
        resource=resource,
        data=dict(data) if isinstance(data, dict) else {},
        output=dict(raw_output) if isinstance(raw_output, dict) else {},
    )
    context = CompensationContext(
        org_id=str(org_id),
        run_id=str(run_id),
        node_id=node_id,
        connector_instance_id=str(instance_id),
    )

    try:
        connector = connector_hub.get(instance_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning(
            "guardrails.compensation.connector_unavailable run=%s node=%s",
            run_id,
            node_id,
            exc_info=True,
        )
        reason = f"connector unavailable: {type(exc).__name__}"
        await _append_attempt_audit(
            session,
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
            resource=resource,
            outcome=CompensationOutcome.FAILED,
            detail=reason,
        )
        return {
            "node_id": node_id,
            "publish_status": "not-compensated",
            "output_ref": {"run_id": str(run_id), "node_id": node_id},
            "compensation": {"outcome": CompensationOutcome.FAILED.value, "reason": reason[:_SUMMARY_REASON_CAP]},
        }

    try:
        result = await connector.compensate(operation, context=context, error=guardrail_block)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("guardrails.compensation.callback_raised run=%s node=%s", run_id, node_id)
        reason = f"compensation callback raised: {type(exc).__name__}"
        await _append_attempt_audit(
            session,
            org_id=org_id,
            run_id=run_id,
            node_id=node_id,
            resource=resource,
            outcome=CompensationOutcome.FAILED,
            detail=reason,
        )
        return {
            "node_id": node_id,
            "publish_status": "not-compensated",
            "output_ref": {"run_id": str(run_id), "node_id": node_id},
            "compensation": {"outcome": CompensationOutcome.FAILED.value, "reason": reason[:_SUMMARY_REASON_CAP]},
        }

    if not isinstance(result, CompensationResult):
        result = CompensationResult(outcome=CompensationOutcome.FAILED, detail="invalid compensation result")
    await _append_attempt_audit(
        session,
        org_id=org_id,
        run_id=run_id,
        node_id=node_id,
        resource=resource,
        outcome=result.outcome,
        detail=result.detail,
    )
    publish_status = "compensated" if result.outcome == CompensationOutcome.COMPENSATED else "not-compensated"
    return {
        "node_id": node_id,
        "publish_status": publish_status,
        "output_ref": {"run_id": str(run_id), "node_id": node_id},
        "compensation": {
            "outcome": result.outcome.value,
            "reason": (result.detail or "")[:_SUMMARY_REASON_CAP],
            "resource_id": result.resource_id,
        },
    }


def _build_summary(
    run: Run,
    guardrail_block: str,
    blocking_eval_name: str,
    executed: dict[str, Any],
    per_node: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the blocked_partial run summary (references, never raw payloads)."""
    return {
        "blocked": True,
        "blocking_eval_name": blocking_eval_name or None,
        "block_message": (guardrail_block or "")[:_SUMMARY_MESSAGE_CAP],
        "run_id": str(run.id),
        "executed_nodes": list(executed),
        "nodes": per_node,
    }


async def compensate_blocked_run(
    session: AsyncSession,
    run: Run,
    guardrail_block: str,
    connector_hub: Any | None = None,
    executed_nodes: dict[str, Any] | None = None,
    *,
    blocking_eval_name: str = "",
) -> dict[str, Any]:
    """Compensate a guardrail-blocked run's external side effects (FAR-213).

    Called AFTER the terminal ``eval_failed``/``eval_blocked`` status write.
    Walks the run's executed node outputs; for each node whose graph binding
    names a connector write resource, invokes the connector's compensating
    callback (via the hub). One node's failure never prevents the others and
    never crashes terminalization — every attempt is logged + audited with a
    summary-only payload, and the whole call is failure-isolated (raises are
    contained here; callers must additionally guard).

    Writes the ``blocked_partial_summary`` column (executed nodes, per-node
    publish status ``published``/``compensated``/``not-compensated``, output
    references) and returns it. RLS org context must already be set on
    *session* by the caller.

    *connector_hub* is optional: when ``None`` (the ingestion-edge wiring,
    where no nodes have executed) only the summary + summary audit are written.
    """
    org_id = run.organisation_id
    executed = _normalise_executed_nodes(run, executed_nodes)

    per_node: list[dict[str, Any]] = []
    if connector_hub is not None and executed:
        graph_nodes = await _load_graph_nodes(session, run)
        node_defs = {str(node.get("id")): node for node in graph_nodes if isinstance(node, dict)}
        for node_id, node_output in executed.items():
            per_node.append(
                await _compensate_one_node(
                    session,
                    org_id=org_id,
                    run_id=run.id,
                    node_id=node_id,
                    node_output=node_output,
                    node_def=node_defs.get(node_id, {}),
                    connector_hub=connector_hub,
                    guardrail_block=guardrail_block,
                )
            )

    summary = _build_summary(run, guardrail_block, blocking_eval_name, executed, per_node)
    try:
        run.blocked_partial_summary = summary
        await session.flush()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.compensation.summary_write_failed run=%s", run.id)

    await _append_summary_audit(
        session,
        org_id=org_id,
        run_id=run.id,
        blocking_eval_name=blocking_eval_name,
        executed=executed,
    )
    return summary
