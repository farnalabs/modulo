"""FAR-613 fire-time HITL gate context capture.

When a HITL gate fires, the executor's interrupt handler builds a BOUNDED
context bundle describing WHY the gate exists and WHAT the reviewer is looking
at, and persists it on the ``hitl_claims.context_json`` column (migration
0182). The bundle is the reviewer's decision briefing source:

* ``description`` — the resolved gate config's human description (edge config
  or node ``hitl_config``).
* ``condition`` — the JMESPath condition expression (edge gates only).
* ``condition_result`` — FAR-688 PRIMARY evidence: the matched value the
  condition evaluated to at fire time ({"expression", "value",
  "evaluated_at_node"}), carried in the interrupt payload by the gate node.
  ``None`` for gates without a condition and for legacy payloads — the
  regex-extracted ``artifacts`` stay the (supplementary) evidence then,
  unchanged.
* ``trigger`` — ``"condition"`` for edge gates, ``"node"`` for FAR-402
  HITL-node gates, ``"unknown"`` when the snapshot cannot resolve either.
* ``source_node_id`` / ``source_node_label`` — the node whose output fed the
  gate (the edge's source / the HITL node itself).
* ``artifacts`` — a bounded excerpt of the outputs relevant to the condition.
* ``reason`` — node gates only: the raising output's ``reason`` field.
* ``pipeline_name`` — resolved by the caller (the failure-isolated seam the
  executor already uses for notifications).

Truncation is DETERMINISTIC: JSON serialised with ``sort_keys=True`` and
``default=str``, then sliced to fixed character budgets — the same gate always
produces the same stored bundle (no wall-clock or dict-order variance). Every
string field is bounded (artifacts ~2KB total; description / condition /
reason / condition value capped at :data:`_TEXT_FIELD_MAX_CHARS`;
``source_node_label`` / ``pipeline_name`` at :data:`_NAME_FIELD_MAX_CHARS`).
Sliced summaries carry the :data:`TRUNCATION_MARKER` suffix so a reviewer can
see an excerpt is partial.

Redaction (FAR-188): artifacts, ``reason`` and the matched condition value are
derived from NODE OUTPUTS / run STATE (LLM / connector content), so they run
through the shared redaction primitive
(:func:`modulo.core.pipeline_engine.error_codes.sanitize_error_text`) BEFORE
truncation — credentials must never enter persistence unmasked. The condition
value is redacted capture-side (``node_runner._serialize_condition_value``)
because it rides in the interrupt payload (which the checkpointer persists);
the builder re-bounds it defensively. User-authored save-time fields
(``description``, ``condition``, ``pipeline_name``, ``source_node_label``) are
bounded but not redacted: they were authored through the validated save path,
not produced by agent/connector output.

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
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.error_codes import sanitize_error_text
from modulo.db.crud.hitl_gate_config import config_from_graph, config_from_hitl_nodes, parse_hitl_gate_id
from modulo.db.crud.run import get_run
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

_log = logging.getLogger(__name__)

#: Total character budget for the ``artifacts`` excerpt (~2KB per FAR-613).
ARTIFACTS_BUDGET_CHARS = 2048
#: Per-entry character budget for a single artifact output summary.
_ARTIFACT_ENTRY_MAX_CHARS = 1200
#: Hard cap on the number of artifact entries (a condition can name many nodes).
_ARTIFACT_MAX_ENTRIES = 5
#: Deterministic cap for the bundle's single-string fields (``description``,
#: ``condition``, ``reason``, the matched condition value). The edge-config
#: contract already caps description at 2000 and condition at 500
#: (api.routes.pipelines HitlGateConfig); this is the capture-side backstop
#: for node-level ``hitl_config`` descriptions (GraphValidator enforces only
#: the minimum) and LLM-derived ``reason`` values, which have no upstream bound.
_TEXT_FIELD_MAX_CHARS = 2000
#: Capture-side cap for human-name fields (``source_node_label``,
#: ``pipeline_name``) — matches the save contracts' 255-char name columns.
_NAME_FIELD_MAX_CHARS = 255
#: Marker appended to a string field that was actually sliced, so a reviewer
#: can tell a truncated excerpt from a complete one (FAR-688).
TRUNCATION_MARKER = "…(truncated)"
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

#: Trigger values recorded in the bundle. ``"unknown"`` (FAR-688) marks a gate
#: whose config (and snapshot node type) cannot be resolved — the trigger is
#: genuinely undeterminable, never guessed.
TRIGGER_CONDITION = "condition"
TRIGGER_NODE = "node"
TRIGGER_UNKNOWN = "unknown"


class HitlGateConditionResult(TypedDict, total=False):
    """The matched condition value as PRIMARY briefing evidence (FAR-688)."""

    #: The expression that was evaluated (fire-time truth).
    expression: str
    #: The serialised, redacted, bounded value the expression matched.
    value: str
    #: The node whose output fed the gate (the gate id's source node).
    evaluated_at_node: str | None


class HitlGateContext(TypedDict, total=False):
    """The ``hitl_claims.context_json`` briefing bundle (FAR-613/FAR-688).

    ``total=False``: legacy bundles persisted before a key existed simply omit
    it; readers must tolerate missing members.
    """

    description: str | None
    condition: str | None
    condition_result: HitlGateConditionResult | None
    trigger: str
    source_node_id: str | None
    source_node_label: str | None
    artifacts: list[dict[str, str]]
    reason: str | None
    pipeline_name: str | None


def serialize_value(value: Any) -> str:
    """Serialise *value* deterministically (``sort_keys=True``, ``default=str``).

    Never raises: ``default=str`` degrades every non-JSON value (UUID,
    datetime, LangChain message objects) to its string form, and any residual
    serialisation error falls back to ``repr``. The ONE deterministic
    serializer for the briefing bundle and its surfaces (FAR-688) — e.g.
    ``node_runner._serialize_condition_value`` reuses it instead of
    re-implementing the body.
    """
    try:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive: default=str makes this near-impossible
        return repr(value)


def slice_with_marker(text: str, cap: int) -> str:
    """Slice *text* to at most *cap* characters, marking a cut (FAR-688).

    Marker-WITHIN-cap semantics: the marker is carved out of the slice, so
    the returned string NEVER exceeds *cap* characters — one length
    semantics for every bounded field across all surfaces (previously some
    call sites sliced to ``cap`` and appended the marker past it, exceeding
    their nominal cap by ``len(TRUNCATION_MARKER)``). A string already
    within the cap is returned unchanged (no marker).
    """
    if len(text) <= cap:
        return text
    head = cap - len(TRUNCATION_MARKER)
    if head <= 0:
        return text[:cap]
    return text[:head] + TRUNCATION_MARKER


def _bound_text(value: Any, cap: int = _TEXT_FIELD_MAX_CHARS) -> str | None:
    """A bounded string form of *value*, or None when unusable.

    Strings are trimmed and sliced to *cap* (with the truncation marker when a
    slice happens — the marker is included WITHIN the cap); non-string
    non-None values are serialised first. None / non-strings that serialise
    to nothing usable return None.
    """
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else serialize_value(value).strip()
    if not text:
        return None
    return slice_with_marker(text, cap)


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

    The remaining budget accounts for the entry's REAL serialised overhead
    (keys, quotes, JSON escaping) — not the raw summary length: slicing a
    summary by raw characters can lengthen the escaped form (an escape
    sequence cut in half), so the sliced entry is re-serialised and, when it
    still overflows, shrunk by the measured overshoot until it fits. A
    sliced summary carries :data:`TRUNCATION_MARKER`.
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
        # JSON overhead of the entry minus the summary itself (keys, quotes,
        # escaping) — the budget the summary actually has left.
        overhead = len(serialized) - len(entry["summary"])
        remaining = ARTIFACTS_BUDGET_CHARS - total - separator - overhead
        if remaining <= 0:
            break
        summary = entry["summary"][:remaining]
        serialized = ""
        while summary:
            candidate = {"node_id": entry["node_id"], "summary": summary + TRUNCATION_MARKER}
            serialized = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
            overflow = total + separator + len(serialized) - ARTIFACTS_BUDGET_CHARS
            if overflow <= 0:
                break
            # Escaping can expand the sliced form — shrink by the measured
            # overshoot and re-serialise. Each pass removes at least one
            # character, so the loop terminates.
            summary = summary[: max(0, len(summary) - overflow)]
        if serialized and total + separator + len(serialized) <= ARTIFACTS_BUDGET_CHARS:
            bounded.append(candidate)
            total += separator + len(serialized)
    return bounded


def _artifact_entry(node_id: str, output: Any) -> dict[str, str]:
    """One bounded, REDACTED artifact entry: the node id plus its output summary.

    Node outputs are agent/connector content, so the summary runs through the
    shared redaction primitive BEFORE truncation (FAR-163: a secret
    straddling the cut point must still be removed; FAR-188: credentials
    never enter persistence unmasked). A sliced summary carries
    :data:`TRUNCATION_MARKER` WITHIN the per-entry cap.
    """
    summary = slice_with_marker(sanitize_error_text(serialize_value(output)), _ARTIFACT_ENTRY_MAX_CHARS)
    return {
        "node_id": node_id,
        "summary": summary,
    }


def _output_reason(output: Any) -> str:
    """The raising output's ``reason`` field, or the documented fallback.

    Both the flat output dict and the outer envelope shape (a node's contract
    output nested under ``output``) are consulted, so a schema-validated
    ``reason`` is found either way. The reason is LLM-derived content, so it
    is REDACTED through the shared primitive before persistence (FAR-188).
    """
    if isinstance(output, dict):
        for candidate in (output, output.get("output")):
            if isinstance(candidate, dict):
                reason = candidate.get("reason")
                if isinstance(reason, str) and reason.strip():
                    return sanitize_error_text(reason)
    return REASON_ABSENT


async def build_hitl_gate_context(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    gate_id: str,
    org_id: uuid.UUID,
    pipeline_name: str | None,
    completed_node_outputs: dict[str, Any] | None,
    condition_result: dict[str, Any] | None = None,
) -> HitlGateContext | None:
    """Build the fire-time briefing bundle for a HITL gate, or ``None``.

    Reads the run's snapshot graph (the graph the run actually executes) to
    resolve the gate's config, trigger kind, and source node. The live
    pipeline definition is NOT consulted — a mid-run edit must not change the
    briefing of a gate that already fired (the snapshot is the authoritative
    fire-time state). ``condition_result`` (FAR-688) is the matched value the
    gate node evaluated — when present on a condition gate it becomes the
    bundle's PRIMARY evidence. Any error logs and returns ``None``: a
    briefing defect must never block the interrupt (failure-isolation
    contract).
    """
    try:
        return await _build_context_inner(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            pipeline_name=pipeline_name,
            completed_node_outputs=completed_node_outputs,
            condition_result=condition_result,
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
    condition_result: dict[str, Any] | None,
) -> HitlGateContext | None:
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
        config = config_from_graph(graph_json, gate_id)
        if config is not None:
            trigger = TRIGGER_CONDITION
        else:
            config = config_from_hitl_nodes(graph_json, gate_id)
            if config is not None:
                trigger = TRIGGER_NODE
    if trigger is None:
        # Config unresolvable (legacy snapshot / graph drift / missing
        # snapshot): infer the trigger from the snapshot node's type when the
        # node is visible; a missing snapshot cannot verify the node's type,
        # so record a neutral "unknown" instead of guessing "condition"
        # (FAR-688 — the old fallback misclassified node gates).
        trigger = TRIGGER_NODE if _snapshot_source_is_hitl_node(graph_json, source_node_id) else TRIGGER_UNKNOWN

    description: str | None = None
    condition: str | None = None
    if isinstance(config, dict):
        raw_description = config.get("description")
        if isinstance(raw_description, str) and raw_description.strip():
            # Bounded: edge-config descriptions are capped at 2000 by the
            # save-time contract; node-level hitl_config descriptions have
            # no upstream max — this slice is the capture-side backstop.
            description = raw_description[:_TEXT_FIELD_MAX_CHARS]
        raw_condition = config.get("condition")
        if trigger == TRIGGER_CONDITION and isinstance(raw_condition, str) and raw_condition.strip():
            condition = raw_condition[:_TEXT_FIELD_MAX_CHARS]

    # FAR-688: PRIMARY evidence — the matched value the gate node evaluated
    # at fire time, carried in the interrupt payload. Recorded whenever the
    # payload carries a usable member — including when the snapshot cannot
    # resolve the gate config (legacy/drift): the payload IS the fire-time
    # truth. Node gates never stamp the member (the runtime only produces it
    # from a JMESPath condition), and a non-dict member is tolerated as no
    # evidence. The payload's expression/value are capture-side redacted +
    # bounded; the builder re-bounds defensively.
    condition_evidence: HitlGateConditionResult | None = None
    if isinstance(condition_result, dict):
        expression = _bound_text(condition_result.get("expression")) or condition
        value = _bound_text(condition_result.get("value"))
        if value is not None:
            condition_evidence = {
                "expression": expression or "",
                "value": value,
                "evaluated_at_node": source_node_id,
            }

    artifacts: list[dict[str, str]] = []
    reason: str | None = None
    if trigger == TRIGGER_NODE:
        node_output = (completed_node_outputs or {}).get(source_node_id or "")
        if node_output is not None:
            reason = _output_reason(node_output)[:_TEXT_FIELD_MAX_CHARS]
            artifacts.append(_artifact_entry(source_node_id or gate_id, node_output))
    else:
        referenced = extract_condition_node_ids(condition)
        if not referenced and source_node_id:
            referenced = [source_node_id]
        for node_id in referenced[:_ARTIFACT_MAX_ENTRIES]:
            output = (completed_node_outputs or {}).get(node_id)
            if output is not None:
                artifacts.append(_artifact_entry(node_id, output))

    label = _snapshot_node_label(graph_json, source_node_id)
    return {
        "description": description,
        "condition": condition,
        "condition_result": condition_evidence,
        "trigger": trigger,
        "source_node_id": source_node_id,
        "source_node_label": label[:_NAME_FIELD_MAX_CHARS] if label else None,
        "artifacts": _bound_artifacts(artifacts),
        "reason": reason,
        "pipeline_name": pipeline_name[:_NAME_FIELD_MAX_CHARS] if pipeline_name else None,
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
    "TRIGGER_CONDITION",
    "TRIGGER_NODE",
    "TRIGGER_UNKNOWN",
    "TRUNCATION_MARKER",
    "HitlGateConditionResult",
    "HitlGateContext",
    "build_hitl_gate_context",
    "extract_condition_node_ids",
    "serialize_value",
    "slice_with_marker",
)
