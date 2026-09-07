"""FAR-613 fire-time HITL gate context capture.

When a HITL gate fires, the executor's interrupt handler builds a BOUNDED
context bundle describing WHY the gate exists and WHAT the reviewer is looking
at, and persists it on the ``hitl_claims.context_json`` column (migration
0182). The bundle is the reviewer's decision briefing source:

* ``description`` — the resolved gate config's human description (edge config
  or node ``hitl_config``).
* ``condition`` — the JMESPath condition expression (edge gates only).
* ``trigger`` — ``"condition"`` for edge gates, ``"node"`` for FAR-402
  HITL-node gates.
* ``source_node_id`` / ``source_node_label`` — the node whose output fed the
  gate (the edge's source / the HITL node itself).
* ``artifacts`` — a bounded excerpt of the outputs relevant to the condition.
* ``reason`` — node gates only: the raising output's ``reason`` field.
* ``pipeline_name`` — resolved by the caller (the failure-isolated seam the
  executor already uses for notifications).

Truncation is DETERMINISTIC: JSON serialised with ``sort_keys=True`` and
``default=str``, then sliced to fixed character budgets — the same gate always
produces the same stored bundle (no wall-clock or dict-order variance).

The capture is FAILURE-ISOLATED by contract: :func:`build_hitl_gate_context`
never raises (any internal error logs and yields ``None`` context) so a
briefing defect can never block or fail the interrupt itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.hitl_gate_config import _config_from_graph, _config_from_hitl_nodes, parse_hitl_gate_id
from modulo.db.crud.run import get_run
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

_log = logging.getLogger(__name__)

#: Total character budget for the ``artifacts`` excerpt (~2KB per FAR-613).
ARTIFACTS_BUDGET_CHARS = 2048
#: Per-entry character budget for a single artifact output summary.
_ARTIFACT_ENTRY_MAX_CHARS = 1200
#: Hard cap on the number of artifact entries (a condition can name many nodes).
_ARTIFACT_MAX_ENTRIES = 5
#: ``reason`` fallback when a node gate's raising output carries no reasoning.
REASON_ABSENT = "no reasoning provided"

#: Node ids referenced by a condition expression. Covers the conventional
#: authoring shapes — a comparison like ``node_id=='<uuid>'`` (the UUID sits
#: inside quotes) and a port-addressed lookup like ``state["<uuid>"]``. Only
#: UUID-shaped ids are extracted (live node ids are UUIDs; bare word keys are
#: ambiguous against the merged state dict).
_NODE_ID_IN_CONDITION_RE = re.compile(
    r"""['"\[]\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*['"\]]"""
)


def _deterministic_json(value: Any, limit: int) -> str:
    """Serialise *value* deterministically, sliced to *limit* characters.

    ``sort_keys=True`` pins the key order, ``default=str`` degrades every
    non-JSON value (UUID, datetime, LangChain message objects) to its string
    form, and the slice makes the budget a hard deterministic bound.
    """
    try:
        text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive: default=str makes this near-impossible
        text = repr(value)
    return text[:limit]


def extract_condition_node_ids(condition: str | None) -> list[str]:
    """Extract UUID node ids referenced by a JMESPath condition expression.

    Order-preserving and deduplicated — the artifacts excerpt follows the
    order the condition references them in.
    """
    if not condition:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _NODE_ID_IN_CONDITION_RE.finditer(condition):
        node_id = match.group(1)
        if node_id in seen:
            continue
        seen.add(node_id)
        ordered.append(node_id)
    return ordered


def _bound_artifacts(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Bound the artifacts excerpt to :data:`ARTIFACTS_BUDGET_CHARS`.

    Deterministic: entries are consumed in the given order; each entry is
    admitted whole while the budget allows, then the first overflowing entry
    has its summary sliced to the remaining budget (or is dropped when even
    its fixed overhead does not fit). The serialised total never exceeds the
    budget.
    """
    bounded: list[dict[str, str]] = []
    total = 2  # the surrounding "[]"
    for entry in entries:
        if len(bounded) >= _ARTIFACT_MAX_ENTRIES:
            break
        separator = 1 if bounded else 0
        serialized = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if total + separator + len(serialized) <= ARTIFACTS_BUDGET_CHARS:
            bounded.append(entry)
            total += separator + len(serialized)
            continue
        # Truncate the summary so the entry fits the remaining budget.
        remaining = ARTIFACTS_BUDGET_CHARS - total - separator - len(serialized) + len(entry["summary"])
        if remaining <= 0:
            break
        truncated = {"node_id": entry["node_id"], "summary": entry["summary"][:remaining]}
        serialized = json.dumps(truncated, sort_keys=True, ensure_ascii=False)
        if total + separator + len(serialized) > ARTIFACTS_BUDGET_CHARS:
            break
        bounded.append(truncated)
        total += separator + len(serialized)
    return bounded


def _artifact_entry(node_id: str, output: Any) -> dict[str, str]:
    """One bounded artifact entry: the node id plus its output summary."""
    return {
        "node_id": node_id,
        "summary": _deterministic_json(output, _ARTIFACT_ENTRY_MAX_CHARS),
    }


def _output_reason(output: Any) -> str:
    """The raising output's ``reason`` field, or the documented fallback.

    Both the flat output dict and the outer envelope shape (a node's contract
    output nested under ``output``) are consulted, so a schema-validated
    ``reason`` is found either way.
    """
    if isinstance(output, dict):
        for candidate in (output, output.get("output")):
            if isinstance(candidate, dict):
                reason = candidate.get("reason")
                if isinstance(reason, str) and reason.strip():
                    return reason
    return REASON_ABSENT


async def build_hitl_gate_context(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    gate_id: str,
    org_id: uuid.UUID,
    pipeline_name: str | None,
    completed_node_outputs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the fire-time briefing bundle for a HITL gate, or ``None``.

    Reads the run's snapshot graph (the graph the run actually executes) to
    resolve the gate's config, trigger kind, and source node. The live
    pipeline definition is NOT consulted — a mid-run edit must not change the
    briefing of a gate that already fired (the snapshot is the authoritative
    fire-time state). Any error logs and returns ``None``: a briefing defect
    must never block the interrupt (failure-isolation contract).
    """
    try:
        return await _build_context_inner(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            pipeline_name=pipeline_name,
            completed_node_outputs=completed_node_outputs,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "hitl_gate.context_capture_failed",
            extra={"run_id": str(run_id), "gate_id": gate_id, "org_id": str(org_id)},
            exc_info=True,
        )
        return None


async def _build_context_inner(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    gate_id: str,
    org_id: uuid.UUID,
    pipeline_name: str | None,
    completed_node_outputs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    run = await get_run(session, run_id, organisation_id=org_id)
    if run is None:
        return None
    graph_json: dict[str, Any] | None = None
    if run.snapshot_id is not None:
        snapshot = (
            await session.execute(
                select(PipelineSnapshot.graph_json).where(
                    PipelineSnapshot.id == run.snapshot_id,
                    PipelineSnapshot.organisation_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if isinstance(snapshot, dict):
            graph_json = snapshot

    parsed = parse_hitl_gate_id(gate_id)
    source_node_id = parsed[0] if parsed else None

    # Resolve the gate config + trigger kind from the snapshot graph. Node
    # gates (FAR-402) carry their config on the source NODE; edge gates on
    # the gated EDGE.
    trigger: str | None = None
    config: dict[str, Any] | None = None
    if graph_json is not None:
        config = _config_from_graph(graph_json, gate_id)
        if config is not None:
            trigger = "condition"
        else:
            config = _config_from_hitl_nodes(graph_json, gate_id)
            if config is not None:
                trigger = "node"
    if trigger is None:
        # Config unresolvable (legacy snapshot / graph drift): persist a
        # minimal bundle so the reviewer still sees the fire-time pipeline
        # name and source node rather than an empty briefing.
        trigger = "node" if _snapshot_source_is_hitl_node(graph_json, source_node_id) else "condition"

    description: str | None = None
    condition: str | None = None
    if isinstance(config, dict):
        raw_description = config.get("description")
        if isinstance(raw_description, str) and raw_description.strip():
            description = raw_description
        raw_condition = config.get("condition")
        if trigger == "condition" and isinstance(raw_condition, str) and raw_condition.strip():
            condition = raw_condition

    artifacts: list[dict[str, str]] = []
    reason: str | None = None
    if trigger == "node":
        node_output = (completed_node_outputs or {}).get(source_node_id or "")
        if node_output is not None:
            reason = _output_reason(node_output)
            artifacts.append(_artifact_entry(source_node_id or gate_id, node_output))
    else:
        referenced = extract_condition_node_ids(condition)
        if not referenced and source_node_id:
            referenced = [source_node_id]
        for node_id in referenced[:_ARTIFACT_MAX_ENTRIES]:
            output = (completed_node_outputs or {}).get(node_id)
            if output is not None:
                artifacts.append(_artifact_entry(node_id, output))

    return {
        "description": description,
        "condition": condition,
        "trigger": trigger,
        "source_node_id": source_node_id,
        "source_node_label": _snapshot_node_label(graph_json, source_node_id),
        "artifacts": _bound_artifacts(artifacts),
        "reason": reason,
        "pipeline_name": pipeline_name,
    }


def _snapshot_source_is_hitl_node(graph_json: dict[str, Any] | None, source_node_id: str | None) -> bool:
    """True when the gate's source node is a FAR-402 HITL node."""
    if graph_json is None or not source_node_id:
        return False
    for node in graph_json.get("nodes", []):
        if isinstance(node, dict) and str(node.get("id", "")) == source_node_id:
            return str(node.get("node_type", "")) == "hitl"
    return False


def _snapshot_node_label(graph_json: dict[str, Any] | None, node_id: str | None) -> str | None:
    """The source node's human label from the snapshot graph (or None)."""
    if graph_json is None or not node_id:
        return None
    for node in graph_json.get("nodes", []):
        if isinstance(node, dict) and str(node.get("id", "")) == node_id:
            label = node.get("label")
            if isinstance(label, str) and label.strip():
                return label
            return None
    return None


__all__ = (
    "ARTIFACTS_BUDGET_CHARS",
    "REASON_ABSENT",
    "build_hitl_gate_context",
    "extract_condition_node_ids",
)
