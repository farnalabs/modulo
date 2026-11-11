"""Deterministic comparison of two determination proposals.

:func:`compare_proposals` takes a *base* side and a *target* side - each a
:class:`~modulo.determination.models.ProposalContract` plus the draft nodes and
edges generated with it - and returns a :class:`ProposalComparison`. It is pure
and side-effect free: nothing here reads or writes a database, activates a
pipeline or persists the comparison. The route is a thin adapter over this
module.

Provenance is never flattened
-----------------------------
A proposal's evidence lives on the contract, not on individual draft nodes or
edges, so an item carries the provenance of the side it came from: ``side``
names that side explicitly, and :class:`ProvenanceBundle` copies that side's
provenance kinds, evidence references, confidence and uncertainty. A changed
item carries BOTH sides' bundles (``base_provenance`` / ``target_provenance``),
so observed/base content can never be read as proposed/target content.

Representability boundary
-------------------------
A proposal contract carries no pipeline nodes or edges; those come from the
determination draft. What the inputs therefore encode about behaviour is
exactly:

* a node's ``node_type`` - the draft generator emits ``manual`` for a human step
  and ``agent`` for an automated one (``placeholder`` is neither);
* an edge's ``hitl_review`` flag - an approval/HITL gate on that transition.

Triggers are not represented by a proposal contract or by draft nodes/edges, so
no trigger category is reported. Automation *suggestions* on the draft response
are recommendations, not represented pipeline behaviour, and are not compared.
Nothing here predicts downstream impact: a category is reported only when the
inputs establish it.

Input integrity is enforced once, by :class:`ProposalComparisonSide`: duplicate
node ids, duplicate edges (same source and target) and edges naming a node
absent from their own side are refused at validation time rather than silently
collapsed by the comparison.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from farna.determination.models import (
    EvidenceReference,
    ProposalConfidence,
    ProposalContract,
    ProvenanceKind,
    ReviewState,
    UnavailableEvidence,
)

#: Which side of the comparison an item came from: the current proposal being
#: migrated from, or the proposed target.
Side = Literal["base", "target"]

#: The kind of draft item a behaviour change refers to.
ItemKind = Literal["node", "edge"]

#: The value types the draft node/edge fields actually carry.
PropertyValue = str | bool | None

BehaviourChangeKind = Literal[
    "approval_gate_added",
    "approval_gate_removed",
    "manual_step_added",
    "manual_step_removed",
    "step_automated",
    "step_deautomated",
]

ComparisonContractVersion = Literal["alpha.determination-comparison.v1"]

COMPARISON_CONTRACT_VERSION: ComparisonContractVersion = "alpha.determination-comparison.v1"

#: ``node_type`` values the draft generator emits for a step (see ``draft.py``).
#: ``placeholder`` (start/end) is neither a manual nor an automated step.
_DRAFT_MANUAL_NODE_TYPE = "manual"
_DRAFT_AUTOMATED_NODE_TYPE = "agent"


class ComparisonNode(BaseModel):
    """A draft pipeline node on one side, mirroring the draft endpoint's shape.

    The draft response's ``required_capabilities`` is a ``ClassVar`` on
    ``DraftNodeResponse`` and is therefore not on the wire, so it is neither
    accepted nor compared here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    node_type: str
    label: str
    connector_type: str | None = None


class ComparisonEdge(BaseModel):
    """A draft pipeline edge on one side, mirroring the draft endpoint's shape."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    edge_type: str = "normal"
    hitl_review: bool = False


class ProposalComparisonSide(BaseModel):
    """One side of a comparison: its proposal contract and its draft graph."""

    model_config = ConfigDict(extra="forbid")

    proposal: ProposalContract
    nodes: list[ComparisonNode] = Field(default_factory=list)
    edges: list[ComparisonEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def draft_topology_is_unambiguous(self) -> ProposalComparisonSide:
        """Refuse a draft graph whose identity or references are ambiguous."""
        duplicate_nodes = sorted(node_id for node_id, count in Counter(node.id for node in self.nodes).items() if count > 1)
        if duplicate_nodes:
            raise ValueError(f"duplicate node ids on one side: {', '.join(duplicate_nodes)}")

        duplicate_edges = sorted(_edge_id(source, target) for (source, target), count in Counter((edge.source, edge.target) for edge in self.edges).items() if count > 1)
        if duplicate_edges:
            raise ValueError(f"duplicate edges on one side: {', '.join(duplicate_edges)}")

        known_node_ids = {node.id for node in self.nodes}
        dangling_edges = sorted(_edge_id(edge.source, edge.target) for edge in self.edges if edge.source not in known_node_ids or edge.target not in known_node_ids)
        if dangling_edges:
            raise ValueError(f"edges reference nodes absent from this side: {', '.join(dangling_edges)}")

        return self


class ProposalComparisonRequest(BaseModel):
    """The two sides of one comparison."""

    model_config = ConfigDict(extra="forbid")

    base: ProposalComparisonSide
    target: ProposalComparisonSide


class ProvenanceBundle(BaseModel):
    """The provenance ONE side's proposal carries, copied onto its items.

    The draft nodes and edges carry no evidence of their own, so this is the
    side's provenance, not item-level attribution: the distinct provenance kinds
    present on the contract's evidence references (first-appearance order), the
    references themselves, and the contract's confidence and uncertainty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance_kinds: list[ProvenanceKind] = Field(default_factory=list)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    confidence: ProposalConfidence
    uncertainty: list[str] = Field(default_factory=list)


class ProposalSideSummary(BaseModel):
    """One side's proposal-level identity and provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    side: Side
    proposal_id: uuid.UUID
    attempt_id: uuid.UUID
    review_state: ReviewState
    activation_status: Literal["not_activated"]
    confidence: ProposalConfidence
    uncertainty: list[str] = Field(default_factory=list)
    provenance: list[EvidenceReference] = Field(default_factory=list)
    unavailable_evidence: list[UnavailableEvidence] = Field(default_factory=list)


class PropertyChange(BaseModel):
    """One changed field, naming the property and both values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    property: str
    base_value: PropertyValue
    target_value: PropertyValue


class NodePresence(BaseModel):
    """A node present on only one side."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    side: Side
    node: ComparisonNode
    provenance: ProvenanceBundle


class NodeModification(BaseModel):
    """A node present on both sides with at least one changed property."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    base: ComparisonNode
    target: ComparisonNode
    changes: list[PropertyChange] = Field(min_length=1)
    base_provenance: ProvenanceBundle
    target_provenance: ProvenanceBundle


class EdgePresence(BaseModel):
    """An edge present on only one side."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    side: Side
    edge: ComparisonEdge
    provenance: ProvenanceBundle


class EdgeModification(BaseModel):
    """An edge present on both sides with at least one changed property."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    base: ComparisonEdge
    target: ComparisonEdge
    changes: list[PropertyChange] = Field(min_length=1)
    base_provenance: ProvenanceBundle
    target_provenance: ProvenanceBundle


class BehaviourChange(BaseModel):
    """A change to represented approval or automation behaviour.

    ``base_value`` / ``target_value`` are the property values that establish the
    category; ``None`` means the edge/node is not present on that side.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BehaviourChangeKind
    subject_kind: ItemKind
    subject_id: str
    property: str
    base_value: PropertyValue
    target_value: PropertyValue


class ProposalComparison(BaseModel):
    """The deterministic, read-only result of comparing two proposals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: ComparisonContractVersion = COMPARISON_CONTRACT_VERSION
    base: ProposalSideSummary
    target: ProposalSideSummary
    nodes_added: list[NodePresence] = Field(default_factory=list)
    nodes_removed: list[NodePresence] = Field(default_factory=list)
    nodes_changed: list[NodeModification] = Field(default_factory=list)
    edges_added: list[EdgePresence] = Field(default_factory=list)
    edges_removed: list[EdgePresence] = Field(default_factory=list)
    edges_changed: list[EdgeModification] = Field(default_factory=list)
    behaviour_changes: list[BehaviourChange] = Field(default_factory=list)


def compare_proposals(base: ProposalComparisonSide, target: ProposalComparisonSide) -> ProposalComparison:
    """Compare *base* with *target* and return the structured difference.

    Node identity is the draft node id; edge identity is ``source->target``. Every
    list is ordered deterministically (nodes and edges by id, behaviour changes
    by kind then subject) and the returned model is frozen. The inputs are not
    mutated, so the same pair always produces the same result.
    """
    base_provenance = _provenance_bundle(base.proposal)
    target_provenance = _provenance_bundle(target.proposal)

    base_nodes = {node.id: node for node in base.nodes}
    target_nodes = {node.id: node for node in target.nodes}
    base_edges = {_edge_id(edge.source, edge.target): edge for edge in base.edges}
    target_edges = {_edge_id(edge.source, edge.target): edge for edge in target.edges}

    nodes_added = _added_nodes(base_nodes, target_nodes, target_provenance)
    nodes_removed = _removed_nodes(base_nodes, target_nodes, base_provenance)
    nodes_changed = _changed_nodes(base_nodes, target_nodes, base_provenance, target_provenance)
    edges_added = _added_edges(base_edges, target_edges, target_provenance)
    edges_removed = _removed_edges(base_edges, target_edges, base_provenance)
    edges_changed = _changed_edges(base_edges, target_edges, base_provenance, target_provenance)

    return ProposalComparison(
        base=_side_summary("base", base.proposal),
        target=_side_summary("target", target.proposal),
        nodes_added=nodes_added,
        nodes_removed=nodes_removed,
        nodes_changed=nodes_changed,
        edges_added=edges_added,
        edges_removed=edges_removed,
        edges_changed=edges_changed,
        behaviour_changes=_behaviour_changes(
            nodes_added=nodes_added,
            nodes_removed=nodes_removed,
            nodes_changed=nodes_changed,
            edges_added=edges_added,
            edges_removed=edges_removed,
            edges_changed=edges_changed,
        ),
    )


def _edge_id(source: str, target: str) -> str:
    """Stable identity of a draft edge (the draft carries no edge id)."""
    return f"{source}->{target}"


def _provenance_bundle(proposal: ProposalContract) -> ProvenanceBundle:
    """Copy the provenance one side's proposal carries."""
    kinds: list[ProvenanceKind] = []
    for reference in proposal.provenance:
        if reference.provenance not in kinds:
            kinds.append(reference.provenance)
    return ProvenanceBundle(
        provenance_kinds=kinds,
        evidence_refs=list(proposal.provenance),
        confidence=proposal.confidence,
        uncertainty=list(proposal.uncertainty),
    )


def _side_summary(side: Side, proposal: ProposalContract) -> ProposalSideSummary:
    """Summarise one side's proposal without copying its nodes or edges."""
    return ProposalSideSummary(
        side=side,
        proposal_id=proposal.proposal_id,
        attempt_id=proposal.attempt.id,
        review_state=proposal.review_state,
        activation_status=proposal.activation_status,
        confidence=proposal.confidence,
        uncertainty=list(proposal.uncertainty),
        provenance=list(proposal.provenance),
        unavailable_evidence=list(proposal.unavailable_evidence),
    )


def _added_nodes(
    base_nodes: dict[str, ComparisonNode],
    target_nodes: dict[str, ComparisonNode],
    target_provenance: ProvenanceBundle,
) -> list[NodePresence]:
    return [NodePresence(id=node_id, side="target", node=target_nodes[node_id], provenance=target_provenance) for node_id in sorted(set(target_nodes) - set(base_nodes))]


def _removed_nodes(
    base_nodes: dict[str, ComparisonNode],
    target_nodes: dict[str, ComparisonNode],
    base_provenance: ProvenanceBundle,
) -> list[NodePresence]:
    return [NodePresence(id=node_id, side="base", node=base_nodes[node_id], provenance=base_provenance) for node_id in sorted(set(base_nodes) - set(target_nodes))]


def _changed_nodes(
    base_nodes: dict[str, ComparisonNode],
    target_nodes: dict[str, ComparisonNode],
    base_provenance: ProvenanceBundle,
    target_provenance: ProvenanceBundle,
) -> list[NodeModification]:
    changed: list[NodeModification] = []
    for node_id in sorted(set(base_nodes) & set(target_nodes)):
        changes = _property_changes(_node_properties(base_nodes[node_id]), _node_properties(target_nodes[node_id]))
        if not changes:
            continue
        changed.append(
            NodeModification(
                id=node_id,
                base=base_nodes[node_id],
                target=target_nodes[node_id],
                changes=changes,
                base_provenance=base_provenance,
                target_provenance=target_provenance,
            )
        )
    return changed


def _added_edges(
    base_edges: dict[str, ComparisonEdge],
    target_edges: dict[str, ComparisonEdge],
    target_provenance: ProvenanceBundle,
) -> list[EdgePresence]:
    return [EdgePresence(id=edge_id, side="target", edge=target_edges[edge_id], provenance=target_provenance) for edge_id in sorted(set(target_edges) - set(base_edges))]


def _removed_edges(
    base_edges: dict[str, ComparisonEdge],
    target_edges: dict[str, ComparisonEdge],
    base_provenance: ProvenanceBundle,
) -> list[EdgePresence]:
    return [EdgePresence(id=edge_id, side="base", edge=base_edges[edge_id], provenance=base_provenance) for edge_id in sorted(set(base_edges) - set(target_edges))]


def _changed_edges(
    base_edges: dict[str, ComparisonEdge],
    target_edges: dict[str, ComparisonEdge],
    base_provenance: ProvenanceBundle,
    target_provenance: ProvenanceBundle,
) -> list[EdgeModification]:
    changed: list[EdgeModification] = []
    for edge_id in sorted(set(base_edges) & set(target_edges)):
        changes = _property_changes(_edge_properties(base_edges[edge_id]), _edge_properties(target_edges[edge_id]))
        if not changes:
            continue
        changed.append(
            EdgeModification(
                id=edge_id,
                base=base_edges[edge_id],
                target=target_edges[edge_id],
                changes=changes,
                base_provenance=base_provenance,
                target_provenance=target_provenance,
            )
        )
    return changed


def _node_properties(node: ComparisonNode) -> dict[str, PropertyValue]:
    """The node fields a comparison reports, in reporting order."""
    return {"node_type": node.node_type, "label": node.label, "connector_type": node.connector_type}


def _edge_properties(edge: ComparisonEdge) -> dict[str, PropertyValue]:
    """The edge fields a comparison reports, in reporting order."""
    return {"edge_type": edge.edge_type, "hitl_review": edge.hitl_review}


def _property_changes(
    base_properties: dict[str, PropertyValue],
    target_properties: dict[str, PropertyValue],
) -> list[PropertyChange]:
    """Name every property whose value differs, with both values."""
    return [PropertyChange(property=name, base_value=value, target_value=target_properties[name]) for name, value in base_properties.items() if target_properties[name] != value]


def _behaviour_changes(
    *,
    nodes_added: list[NodePresence],
    nodes_removed: list[NodePresence],
    nodes_changed: list[NodeModification],
    edges_added: list[EdgePresence],
    edges_removed: list[EdgePresence],
    edges_changed: list[EdgeModification],
) -> list[BehaviourChange]:
    """Approval/HITL and automation categories the inputs establish."""
    changes = [
        *_edge_gate_changes(edges_added, edges_removed, edges_changed),
        *_node_automation_changes(nodes_added, nodes_removed, nodes_changed),
    ]
    return sorted(changes, key=lambda change: (change.kind, change.subject_kind, change.subject_id))


def _edge_gate_changes(
    edges_added: list[EdgePresence],
    edges_removed: list[EdgePresence],
    edges_changed: list[EdgeModification],
) -> list[BehaviourChange]:
    """Approval gates gained, lost, or removed with their whole edge."""
    changes: list[BehaviourChange] = [_gate_change("approval_gate_added", added.id, None, True) for added in edges_added if added.edge.hitl_review]
    changes.extend(_gate_change("approval_gate_removed", removed.id, True, None) for removed in edges_removed if removed.edge.hitl_review)
    for modified in edges_changed:
        hitl_change = next((change for change in modified.changes if change.property == "hitl_review"), None)
        if hitl_change is None:
            continue
        kind: BehaviourChangeKind = "approval_gate_added" if hitl_change.target_value is True else "approval_gate_removed"
        changes.append(_gate_change(kind, modified.id, hitl_change.base_value, hitl_change.target_value))
    return changes


def _gate_change(
    kind: BehaviourChangeKind,
    subject_id: str,
    base_value: PropertyValue,
    target_value: PropertyValue,
) -> BehaviourChange:
    return BehaviourChange(
        kind=kind,
        subject_kind="edge",
        subject_id=subject_id,
        property="hitl_review",
        base_value=base_value,
        target_value=target_value,
    )


def _node_automation_changes(
    nodes_added: list[NodePresence],
    nodes_removed: list[NodePresence],
    nodes_changed: list[NodeModification],
) -> list[BehaviourChange]:
    """Manual steps gained or lost, and steps moving between manual and agent."""
    changes: list[BehaviourChange] = [_automation_change("manual_step_added", added.id, None, added.node.node_type) for added in nodes_added if added.node.node_type == _DRAFT_MANUAL_NODE_TYPE]
    changes.extend(_automation_change("manual_step_removed", removed.id, removed.node.node_type, None) for removed in nodes_removed if removed.node.node_type == _DRAFT_MANUAL_NODE_TYPE)
    for modified in nodes_changed:
        node_type_change = next((change for change in modified.changes if change.property == "node_type"), None)
        if node_type_change is None:
            continue
        base_type = node_type_change.base_value
        target_type = node_type_change.target_value
        if base_type == _DRAFT_MANUAL_NODE_TYPE and target_type == _DRAFT_AUTOMATED_NODE_TYPE:
            changes.append(_automation_change("step_automated", modified.id, base_type, target_type))
        elif base_type == _DRAFT_AUTOMATED_NODE_TYPE and target_type == _DRAFT_MANUAL_NODE_TYPE:
            changes.append(_automation_change("step_deautomated", modified.id, base_type, target_type))
    return changes


def _automation_change(
    kind: BehaviourChangeKind,
    subject_id: str,
    base_value: PropertyValue,
    target_value: PropertyValue,
) -> BehaviourChange:
    return BehaviourChange(
        kind=kind,
        subject_kind="node",
        subject_id=subject_id,
        property="node_type",
        base_value=base_value,
        target_value=target_value,
    )
