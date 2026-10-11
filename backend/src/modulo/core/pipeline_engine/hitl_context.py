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

The capture is FAILURE-ISOLATED by contract: :func:`build_hitl_review_context`
never raises (any internal error logs and yields ``None`` context) so a
briefing defect can never block or fail the interrupt itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.error_codes import sanitize_error_text
from modulo.db.crud.hitl_review_config import (
    config_from_graph,
    config_from_hitl_nodes,
    edge_source_or_target,
    make_review_id,
    parse_hitl_review_id,
)
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
#: (api.routes.pipelines HitlReviewConfig); this is the capture-side backstop
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


class HitlReviewConditionResult(TypedDict, total=False):
    """The matched condition value as PRIMARY briefing evidence (FAR-688)."""

    #: The expression that was evaluated (fire-time truth).
    expression: str
    #: The serialised, redacted, bounded value the expression matched.
    value: str
    #: The node whose output fed the gate (the gate id's source node).
    evaluated_at_node: str | None


class HitlReviewContext(TypedDict, total=False):
    """The ``hitl_claims.context_json`` briefing bundle (FAR-613/FAR-688/FAR-859/FAR-860).

    ``total=False``: legacy bundles persisted before a key existed simply omit
    it; readers must tolerate missing members.
    """

    description: str | None
    condition: str | None
    condition_result: HitlReviewConditionResult | None
    trigger: str
    source_node_id: str | None
    source_node_label: str | None
    artifacts: list[dict[str, str]]
    reason: str | None
    pipeline_name: str | None
    subject: str | None
    #: FAR-862: the container that holds the subject value in state, so the
    #: frontend can reconstruct a shape-preserving modified_output (replacing
    #: only the subject leaf). ``None`` when the parent cannot be resolved
    #: (complex JMESPath, non-dict parent, capture failure) — the frontend
    #: hides the editor in that case (fail-safe: no lossy partial dict).
    subject_parent: dict[str, Any] | None
    #: FAR-862: the key in ``subject_parent`` whose value is the subject.
    subject_leaf_key: str | None
    consequences: dict[str, Any] | None
    #: FAR-860: the gate's response contract (agent-defined options).
    #: Threaded from the gate config at fire time so the UI can render
    #: the option set from the briefing without re-resolving the graph.
    response_contract: dict[str, Any] | None


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


async def build_hitl_review_context(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    review_id: str,
    org_id: uuid.UUID,
    pipeline_name: str | None,
    completed_node_outputs: dict[str, Any] | None,
    condition_result: dict[str, Any] | None = None,
    subject: str | None = None,
    subject_parent: dict[str, Any] | None = None,
    subject_leaf_key: str | None = None,
) -> HitlReviewContext | None:
    """Build the fire-time briefing bundle for a HITL gate, or ``None``.

    Reads the run's snapshot graph (the graph the run actually executes) to
    resolve the gate's config, trigger kind, and source node. The live
    pipeline definition is NOT consulted — a mid-run edit must not change the
    briefing of a gate that already fired (the snapshot is the authoritative
    fire-time state). ``condition_result`` (FAR-688) is the matched value the
    gate node evaluated — when present on a condition gate it becomes the
    bundle's PRIMARY evidence. ``subject`` (FAR-859) is the pre-resolved
    value at the gate's ``subject_path`` — the caller resolves it against the
    run state (the same root the condition evaluates against) so the builder
    does not need the state dict. ``subject_parent`` / ``subject_leaf_key``
    (FAR-862) are the container holding the subject and the key within it, so
    the frontend can reconstruct a shape-preserving ``modified_output``. Any
    error logs and returns ``None``: a briefing defect must never block the
    interrupt (failure-isolation contract).
    """
    try:
        return await _build_context_inner(
            session,
            run_id=run_id,
            review_id=review_id,
            org_id=org_id,
            pipeline_name=pipeline_name,
            completed_node_outputs=completed_node_outputs,
            condition_result=condition_result,
            subject=subject,
            subject_parent=subject_parent,
            subject_leaf_key=subject_leaf_key,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "hitl_review.context_capture_failed",
            extra={"run_id": str(run_id), "review_id": review_id, "org_id": str(org_id)},
            exc_info=True,
        )
        return None


@dataclass(frozen=True)
class ReviewFrame:
    """The gate's frozen fire-time resolution: run + snapshot + captured config.

    Built ONLY from the run row and its FROZEN snapshot graph — the live
    pipeline definition is never consulted (a mid-run edit must not change
    the briefing of a gate that already fired).
    """

    run_id: uuid.UUID
    org_id: uuid.UUID
    review_id: str
    source_node_id: str | None
    graph_json: dict[str, Any] | None
    trigger: str
    config: dict[str, Any] | None
    description: str | None
    condition: str | None


@dataclass(frozen=True)
class ActorFrame:
    """Caller-supplied fire-time provenance: actor outputs and subject values.

    Pure carry — no DB access. ``subject`` / ``subject_parent`` stay raw here;
    :func:`_render_review_payload` redacts and bounds them.
    """

    pipeline_name: str | None
    condition_result: dict[str, Any] | None
    artifacts: list[dict[str, str]]
    reason: str | None
    subject: str | None
    subject_parent: dict[str, Any] | None
    subject_leaf_key: str | None


async def _load_snapshot_graph(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID | None,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    review_id: str,
) -> dict[str, Any] | None:
    """The run's FROZEN snapshot graph, door-normalised (FAR-1486), or None.

    Reads ``pipeline_snapshots.graph_json`` — never the live pipeline. A
    malformed ``edges`` / ``nodes`` member is normalised to an empty list
    WITH a log, so the walkers below never see a non-iterable.
    """
    if snapshot_id is None:
        return None
    snapshot = (
        await session.execute(
            select(PipelineSnapshot.graph_json).where(
                PipelineSnapshot.id == snapshot_id,
                PipelineSnapshot.organisation_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if not isinstance(snapshot, dict):
        return None
    graph_json = snapshot
    # FAR-1486: degrade a malformed snapshot ONCE at the door. Every walker
    # below (``config_from_graph`` first of all) iterates ``edges``
    # unconditionally, so a snapshot whose ``edges`` is not a list (``null`` /
    # a scalar) would raise into the outer capture guard and null the ENTIRE
    # briefing. Normalise to an empty edge list instead — best-effort capture
    # fails open WITH a log.
    if not isinstance(graph_json.get("edges", []), list):
        _log.warning(
            "hitl_review.malformed_snapshot_edges",
            extra={"run_id": str(run_id), "review_id": review_id, "org_id": str(org_id)},
        )
        graph_json = {**graph_json, "edges": []}
    # Same rule for ``nodes``: every node walker below
    # (``config_from_hitl_nodes``, ``_snapshot_source_is_hitl_node``,
    # ``_snapshot_node_label``, and through it ``_resolve_consequences``)
    # iterates ``nodes`` unconditionally, so a snapshot whose ``nodes`` is not
    # a list (``null`` / a scalar) would raise into the outer capture guard
    # and null the ENTIRE briefing. Normalise to an empty node list instead —
    # best-effort capture fails open WITH a log, so labels resolve to null
    # rather than the briefing being lost.
    if not isinstance(graph_json.get("nodes", []), list):
        _log.warning(
            "hitl_review.malformed_snapshot_nodes",
            extra={"run_id": str(run_id), "review_id": review_id, "org_id": str(org_id)},
        )
        graph_json = {**graph_json, "nodes": []}
    return graph_json


def _config_and_trigger_from_graph(
    graph_json: dict[str, Any],
    review_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """The gate config + trigger kind carried by the SNAPSHOT graph, or None.

    Edge gates carry their config on the gated EDGE; node gates (FAR-402)
    carry it on the source NODE. ``None`` trigger means neither resolved
    (legacy snapshot / graph drift).
    """
    config = config_from_graph(graph_json, review_id)
    if config is not None:
        return config, TRIGGER_CONDITION
    config = config_from_hitl_nodes(graph_json, review_id)
    if config is not None:
        return config, TRIGGER_NODE
    return None, None


def _resolve_gate_config(
    graph_json: dict[str, Any] | None,
    review_id: str,
    source_node_id: str | None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the gate config + trigger kind from the SNAPSHOT graph.

    Config unresolvable (legacy snapshot / graph drift / missing snapshot):
    infer the trigger from the snapshot node's type when the node is
    visible; a missing snapshot cannot verify the node's type, so record a
    neutral "unknown" instead of guessing "condition" (FAR-688 — the old
    fallback misclassified node gates).
    """
    trigger: str | None = None
    config: dict[str, Any] | None = None
    if graph_json is not None:
        config, trigger = _config_and_trigger_from_graph(graph_json, review_id)
    if trigger is None:
        trigger = TRIGGER_NODE if _snapshot_source_is_hitl_node(graph_json, source_node_id) else TRIGGER_UNKNOWN
    return config, trigger


def _bounded_description(config: dict[str, Any] | None) -> str | None:
    """The bounded ``description`` captured from the gate config.

    Bounded: edge-config descriptions are capped at 2000 by the save-time
    contract; node-level hitl_config descriptions have no upstream max —
    this slice is the capture-side backstop.
    """
    if not isinstance(config, dict):
        return None
    raw_description = config.get("description")
    if isinstance(raw_description, str) and raw_description.strip():
        return raw_description[:_TEXT_FIELD_MAX_CHARS]
    return None


def _bounded_condition(config: dict[str, Any] | None, trigger: str) -> str | None:
    """The bounded ``condition`` — edge gates only (the only kind that has one)."""
    if trigger != TRIGGER_CONDITION or not isinstance(config, dict):
        return None
    raw_condition = config.get("condition")
    if isinstance(raw_condition, str) and raw_condition.strip():
        return raw_condition[:_TEXT_FIELD_MAX_CHARS]
    return None


async def _load_review_frame(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    review_id: str,
    org_id: uuid.UUID,
) -> ReviewFrame | None:
    """Resolve the run, its FROZEN snapshot, and the captured review config.

    The review config is read from the run's snapshot graph — the live
    pipeline definition is NEVER consulted (frozen-record rule). Returns
    ``None`` only for the expected miss (the run does not exist); any other
    failure (DB errors, malformed config) RAISES and is caught by the outer
    fail-isolation in :func:`build_hitl_review_context`.
    """
    run = await get_run(session, run_id, organisation_id=org_id)
    if run is None:
        return None
    graph_json = await _load_snapshot_graph(
        session,
        snapshot_id=run.snapshot_id,
        org_id=org_id,
        run_id=run_id,
        review_id=review_id,
    )
    parsed = parse_hitl_review_id(review_id)
    source_node_id = parsed[0] if parsed else None
    config, trigger = _resolve_gate_config(graph_json, review_id, source_node_id)
    description = _bounded_description(config)
    condition = _bounded_condition(config, trigger)
    return ReviewFrame(
        run_id=run_id,
        org_id=org_id,
        review_id=review_id,
        source_node_id=source_node_id,
        graph_json=graph_json,
        trigger=trigger,
        config=config,
        description=description,
        condition=condition,
    )


def _node_gate_artifacts(
    frame: ReviewFrame,
    outputs: dict[str, Any],
) -> tuple[list[dict[str, str]], str | None]:
    """Node gate (FAR-402): the raising node's own output + its reason."""
    node_output = outputs.get(frame.source_node_id or "")
    if node_output is None:
        return [], None
    reason = _output_reason(node_output)[:_TEXT_FIELD_MAX_CHARS]
    entry = _artifact_entry(frame.source_node_id or frame.review_id, node_output)
    return [entry], reason


def _condition_artifacts(
    frame: ReviewFrame,
    outputs: dict[str, Any],
) -> tuple[list[dict[str, str]], str | None]:
    """Edge gate: the outputs the condition references (else the source node)."""
    referenced = extract_condition_node_ids(frame.condition)
    if not referenced and frame.source_node_id:
        referenced = [frame.source_node_id]
    artifacts: list[dict[str, str]] = []
    for node_id in referenced[:_ARTIFACT_MAX_ENTRIES]:
        output = outputs.get(node_id)
        if output is not None:
            artifacts.append(_artifact_entry(node_id, output))
    return artifacts, None


def _collect_node_artifacts(
    frame: ReviewFrame,
    completed_node_outputs: dict[str, Any] | None,
) -> tuple[list[dict[str, str]], str | None]:
    """The REDACTED, bounded artifact excerpt plus the node-gate reason."""
    outputs = completed_node_outputs or {}
    if frame.trigger == TRIGGER_NODE:
        return _node_gate_artifacts(frame, outputs)
    return _condition_artifacts(frame, outputs)


def _load_actor_frame(
    frame: ReviewFrame,
    *,
    pipeline_name: str | None,
    completed_node_outputs: dict[str, Any] | None,
    condition_result: dict[str, Any] | None,
    subject: str | None,
    subject_parent: dict[str, Any] | None,
    subject_leaf_key: str | None,
) -> ActorFrame:
    """Carry the caller-supplied fire-time provenance alongside the artifacts.

    Pure assembly — no DB access, no redaction (``_render_review_payload``
    bounds/redacts the subject values; the artifact summaries are already
    redacted by :func:`_artifact_entry`).
    """
    artifacts, reason = _collect_node_artifacts(frame, completed_node_outputs)
    return ActorFrame(
        pipeline_name=pipeline_name,
        condition_result=condition_result if isinstance(condition_result, dict) else None,
        artifacts=artifacts,
        reason=reason,
        subject=subject,
        subject_parent=subject_parent if isinstance(subject_parent, dict) else None,
        subject_leaf_key=subject_leaf_key,
    )


def _condition_evidence(frame: ReviewFrame, actor: ActorFrame) -> HitlReviewConditionResult | None:
    """FAR-688: PRIMARY evidence — the matched value the gate node evaluated.

    Recorded whenever the payload carries a usable member — including when
    the snapshot cannot resolve the gate config (legacy/drift): the payload
    IS the fire-time truth. Node gates never stamp the member (the runtime
    only produces it from a JMESPath condition), and a non-dict member is
    tolerated as no evidence. The payload's expression/value are capture-side
    redacted + bounded; the builder re-bounds defensively.
    """
    condition_result = actor.condition_result
    if not isinstance(condition_result, dict):
        return None
    expression = _bound_text(condition_result.get("expression")) or frame.condition
    value = _bound_text(condition_result.get("value"))
    if value is None:
        return None
    return {
        "expression": expression or "",
        "value": value,
        "evaluated_at_node": frame.source_node_id,
    }


def _bound_subject(actor: ActorFrame) -> str | None:
    """The bounded, redacted subject (FAR-859).

    The subject is pre-serialised by the caller (node_runner) as a string;
    redact and bound without re-serialising to avoid double-quoting.
    """
    if actor.subject is None:
        return None
    return _bound_text(sanitize_error_text(actor.subject))


def _bound_subject_parent(actor: ActorFrame) -> tuple[dict[str, Any] | None, str | None]:
    """The bounded, redacted ``(subject_parent, subject_leaf_key)`` (FAR-862).

    The parent container holding the subject lets the frontend reconstruct a
    shape-preserving modified_output — bounded and redacted identically to
    artifacts. Only JSON-serialisable dicts are captured; capture failure
    yields None (fail-safe).
    """
    subject_parent = actor.subject_parent
    subject_leaf_key = actor.subject_leaf_key
    if not isinstance(subject_parent, dict) or subject_leaf_key is None:
        return None, None
    try:
        raw_parent = serialize_value(subject_parent)
        redacted_parent = sanitize_error_text(raw_parent)
        bounded_parent_str = slice_with_marker(redacted_parent, ARTIFACTS_BUDGET_CHARS)
        bounded_parent = json.loads(bounded_parent_str) if bounded_parent_str else None
        return bounded_parent, subject_leaf_key[:_NAME_FIELD_MAX_CHARS]
    except (TypeError, ValueError, KeyError):
        return None, None


def _consequences(frame: ReviewFrame) -> dict[str, Any] | None:
    """FAR-859/1486/1487: approve/reject routing from the snapshot graph.

    Best-effort, fail OPEN WITH A LOG — a defect INSIDE the resolver (a
    malformed collection reached through _snapshot_node_label, a snapshot
    shape the door-normalisation did not catch) must degrade to no
    consequences rather than raise out and null the whole briefing (the
    outer capture guard would otherwise return None and lose every other
    field).
    """
    graph_json = frame.graph_json
    if graph_json is None:
        return None
    try:
        return _resolve_consequences(graph_json, frame.review_id, frame.config, frame.source_node_id)
    except Exception:
        _log.warning(
            "hitl_review.consequences_resolution_failed",
            extra={"run_id": str(frame.run_id), "review_id": frame.review_id, "org_id": str(frame.org_id)},
            exc_info=True,
        )
        return None


def _response_contract(frame: ReviewFrame) -> dict[str, Any] | None:
    """FAR-860: the gate's response contract (agent-defined options).

    Captured from the gate config so the UI can render agent-defined options
    from the briefing without re-resolving the graph. Bounded: only the
    serialisable subset is captured; the Pydantic model already enforces
    field limits.
    """
    config = frame.config
    if not isinstance(config, dict):
        return None
    raw_rc = config.get("response_contract")
    if not isinstance(raw_rc, dict):
        return None
    # Bounded copy: re-serialise to strip any extra keys and bound.
    try:
        contract: dict[str, Any] = json.loads(json.dumps(raw_rc, sort_keys=True, default=str, ensure_ascii=False))
    except (TypeError, ValueError, KeyError):
        return None
    return contract


def _render_review_payload(frame: ReviewFrame, actor: ActorFrame) -> HitlReviewContext:
    """Pure assembly of the returned briefing bundle from the two frames."""
    condition_evidence = _condition_evidence(frame, actor)
    bounded_subject = _bound_subject(actor)
    bounded_subject_parent, bounded_subject_leaf_key = _bound_subject_parent(actor)
    label = _snapshot_node_label(frame.graph_json, frame.source_node_id)
    return {
        "description": frame.description,
        "condition": frame.condition,
        "condition_result": condition_evidence,
        "trigger": frame.trigger,
        "source_node_id": frame.source_node_id,
        "source_node_label": label[:_NAME_FIELD_MAX_CHARS] if label else None,
        "artifacts": _bound_artifacts(actor.artifacts),
        "reason": actor.reason,
        "pipeline_name": actor.pipeline_name[:_NAME_FIELD_MAX_CHARS] if actor.pipeline_name else None,
        "subject": bounded_subject,
        "subject_parent": bounded_subject_parent,
        "subject_leaf_key": bounded_subject_leaf_key,
        "consequences": _consequences(frame),
        "response_contract": _response_contract(frame),
    }


async def _build_context_inner(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    review_id: str,
    org_id: uuid.UUID,
    pipeline_name: str | None,
    completed_node_outputs: dict[str, Any] | None,
    condition_result: dict[str, Any] | None,
    subject: str | None,
    subject_parent: dict[str, Any] | None,
    subject_leaf_key: str | None,
) -> HitlReviewContext | None:
    """Thin orchestrator: load the frames, render the payload (S3776 split).

    Fail isolation lives ONLY in the outer :func:`build_hitl_review_context`;
    every helper here is allowed to raise.
    """
    frame = await _load_review_frame(session, run_id=run_id, review_id=review_id, org_id=org_id)
    if frame is None:
        return None
    actor = _load_actor_frame(
        frame,
        pipeline_name=pipeline_name,
        completed_node_outputs=completed_node_outputs,
        condition_result=condition_result,
        subject=subject,
        subject_parent=subject_parent,
        subject_leaf_key=subject_leaf_key,
    )
    return _render_review_payload(frame, actor)


# FAR-1487: what a HITL rejection does. ``route`` = divert to a reject
# destination, ``terminate`` = END the run ``rejected`` (the DEFAULT when no
# route exists), ``proceed`` = continue down the approve path (explicit only).
REJECT_DISPOSITION_ROUTE = "route"
REJECT_DISPOSITION_TERMINATE = "terminate"
REJECT_DISPOSITION_PROCEED = "proceed"
ON_REJECT_PROCEED = "proceed"
ON_REJECT_TERMINATE = "terminate"


def resolve_reject_disposition(config: dict[str, Any] | None, *, has_reject_route: bool) -> str:
    """The single source of truth for what a HITL rejection does (FAR-1487).

    Shared by the graph compiler (which wires the edge) and the reviewer
    briefing (which states the consequence) so they can never disagree.

    Precedence: a reject destination (``reject_target`` config or reject edge)
    ROUTES; otherwise an automated ``correction_target`` WINS over terminating
    (an explicit correction chooses "rework" - the rejection dispatches the
    correction and the run continues as before); otherwise an explicit
    ``on_reject: proceed`` continues; otherwise the run TERMINATES. Continuing
    is never the silent fallback.
    """
    if has_reject_route:
        return REJECT_DISPOSITION_ROUTE
    cfg = config if isinstance(config, dict) else {}
    if cfg.get("correction_target"):
        return REJECT_DISPOSITION_PROCEED
    if cfg.get("on_reject") == ON_REJECT_PROCEED:
        return REJECT_DISPOSITION_PROCEED
    return REJECT_DISPOSITION_TERMINATE


def _resolve_consequences(
    graph_json: dict[str, Any],
    review_id: str,
    config: dict[str, Any] | None,
    source_node_id: str | None,
) -> dict[str, Any] | None:
    """Resolve approve/reject consequences from the snapshot graph.

    Approve: the gate edge's ``target_node_id`` (the run continues there).
    Reject: the gate config's ``reject_target``, else a reject-typed edge from
    the gate's source node — the same precedence the graph compiler applies.

    A FALSY config value (``""`` / ``False``) never yields a reject route:
    the compiler's ``if reject_target:`` wires no router for it, and its
    ``is None`` fallback check means such a value also blocks the reject-edge
    lookup — so emit no reject consequence rather than a phantom
    ``{"node_id": "False"}`` entry.

    Returns ``None`` when neither target can be resolved.
    """
    approve_target: str | None = None
    reject_target: str | None = None
    gate_source: str | None = None

    # Approve: find the edge whose topology-derived review_id matches and
    # extract its target.  Uses make_review_id (topology-based) — the same
    # derivation the executor stamps at fire time — NOT the config's
    # review_id field, which is injected only in compiled graphs.
    for edge in graph_json.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source = edge_source_or_target(edge, "source")
        target = edge_source_or_target(edge, "target")
        if source is not None and target is not None and make_review_id(source, target) == review_id:
            approve_target = str(target)
            gate_source = source
            break

    # Reject: gate config ``reject_target`` first, then a reject-typed edge
    # from the gate's source node — the SAME precedence the graph compiler
    # applies (graph_cache._add_hitl_review_edge: config value, ``is None``
    # → reject_targets_by_source, then ``if reject_target:`` wires the
    # router). An edge-wired reject route with no config reject_target must
    # still show a reject consequence in the reviewer briefing.
    raw_reject: Any = None
    if isinstance(config, dict):
        raw_reject = config.get("reject_target")
    if raw_reject is None:
        reject_source = gate_source if gate_source is not None else source_node_id
        if reject_source is not None:
            for edge in graph_json.get("edges", []):
                if not isinstance(edge, dict):
                    continue
                # Mirror graph_cache._get_edge_type (type, else edge_type).
                edge_type = edge.get("type", edge.get("edge_type", ""))
                if edge_type is None or str(edge_type) != "reject":
                    continue
                if edge_source_or_target(edge, "source") == reject_source:
                    target = edge_source_or_target(edge, "target")
                    if target:
                        # Last matching reject edge wins — mirrors
                        # graph_cache._build_reject_targets' dict build.
                        reject_target = target
    elif raw_reject:
        reject_target = str(raw_reject)

    if approve_target is None and reject_target is None and not isinstance(config, dict):
        return None

    result: dict[str, Any] = {}
    if approve_target is not None:
        result["approve"] = {
            "node_id": approve_target,
            "label": _snapshot_node_label(graph_json, approve_target),
        }
    # FAR-1487: the reject consequence is ALWAYS stated, in all three
    # dispositions — route (a reject destination exists), terminate (the
    # default: the run ENDS ``rejected``) and proceed (explicit
    # ``on_reject: proceed`` / an automated ``correction_target``: the run
    # continues down the approve path). A reviewer must never have to guess
    # what "reject" does.
    disposition = resolve_reject_disposition(config, has_reject_route=bool(reject_target))
    reject: dict[str, Any] = {"disposition": disposition}
    if reject_target is not None:
        reject["node_id"] = reject_target
        reject["label"] = _snapshot_node_label(graph_json, reject_target)
    result["reject"] = reject
    return result


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
    "REJECT_DISPOSITION_PROCEED",
    "REJECT_DISPOSITION_ROUTE",
    "REJECT_DISPOSITION_TERMINATE",
    "TRIGGER_CONDITION",
    "TRIGGER_NODE",
    "TRIGGER_UNKNOWN",
    "TRUNCATION_MARKER",
    "HitlReviewConditionResult",
    "HitlReviewContext",
    "build_hitl_review_context",
    "extract_condition_node_ids",
    "resolve_reject_disposition",
    "serialize_value",
    "slice_with_marker",
)
