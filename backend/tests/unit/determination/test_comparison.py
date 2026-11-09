"""Tests for the deterministic determination proposal comparison (FAR-2473).

Covers added/removed/changed nodes and edges, per-property changes with both
values, the approval/HITL and automation categories the draft graph encodes,
provenance surviving on both sides, empty and identical inputs, determinism,
input immutability and the input-integrity validation.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from modulo.determination.comparison import (
    ComparisonEdge,
    ComparisonNode,
    ProposalComparison,
    ProposalComparisonSide,
    compare_proposals,
)
from modulo.determination.models import (
    DeterminationAttempt,
    EvidenceReference,
    ProposalConfidence,
    ProposalContract,
    ProposalFact,
    ProvenanceKind,
    ReviewState,
    SourceInventoryItem,
    UnavailableEvidence,
)

_PROPOSAL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TARGET_PROPOSAL_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")
_ATTEMPT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_FACT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _evidence(
    reference_id: str = "evidence:github-1:repos",
    *,
    kind: ProvenanceKind = "observed",
    source_id: str = "github-1:repos",
) -> EvidenceReference:
    return EvidenceReference(reference_id=reference_id, source_id=source_id, locator="scan:repos", provenance=kind)


def _contract(
    *,
    proposal_id: uuid.UUID = _PROPOSAL_ID,
    confidence: ProposalConfidence = "medium",
    uncertainty: list[str] | None = None,
    provenance: list[EvidenceReference] | None = None,
    unavailable: list[UnavailableEvidence] | None = None,
    observed: list[ProposalFact] | None = None,
    derived: list[ProposalFact] | None = None,
    review_state: ReviewState = "needs_review",
) -> ProposalContract:
    return ProposalContract(
        proposal_id=proposal_id,
        attempt=DeterminationAttempt(
            id=_ATTEMPT_ID,
            status="completed",
            source_inventory=[
                SourceInventoryItem(
                    source_id="github-1:repos",
                    connector_type="github",
                    resource="repos",
                    sample_count=1,
                    status="available",
                )
            ],
        ),
        review_state=review_state,
        observed_facts=observed
        if observed is not None
        else [
            ProposalFact(
                id=_FACT_ID,
                statement="Observed 1 repository",
                kind="observed",
                confidence="high",
                evidence_refs=[_evidence()],
            )
        ],
        derived_elements=derived if derived is not None else [],
        unavailable_evidence=unavailable if unavailable is not None else [],
        confidence=confidence,
        uncertainty=uncertainty if uncertainty is not None else [],
        provenance=provenance if provenance is not None else [_evidence()],
    )


def _side(
    *,
    contract: ProposalContract | None = None,
    nodes: list[ComparisonNode] | None = None,
    edges: list[ComparisonEdge] | None = None,
) -> ProposalComparisonSide:
    return ProposalComparisonSide(
        proposal=contract if contract is not None else _contract(),
        nodes=list(nodes or []),
        edges=list(edges or []),
    )


def _node(
    node_id: str = "development",
    *,
    node_type: str = "agent",
    label: str = "Development (Code Generation)",
    connector_type: str | None = "github",
) -> ComparisonNode:
    return ComparisonNode(id=node_id, node_type=node_type, label=label, connector_type=connector_type)


def _edge(
    source: str = "start",
    target: str = "development",
    *,
    edge_type: str = "normal",
    hitl_review: bool = False,
) -> ComparisonEdge:
    return ComparisonEdge(source=source, target=target, edge_type=edge_type, hitl_review=hitl_review)


def _behaviour_kinds(comparison: ProposalComparison) -> list[str]:
    return [change.kind for change in comparison.behaviour_changes]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def test_identical_sides_produce_no_changes() -> None:
    nodes = [
        _node("start", node_type="placeholder", label="Start", connector_type=None),
        _node(),
        _node("review", label="Code Review", connector_type="github"),
    ]
    edges = [_edge("start", "development"), _edge("development", "review", hitl_review=True)]

    comparison = compare_proposals(_side(nodes=nodes, edges=edges), _side(nodes=nodes, edges=edges))

    assert not comparison.nodes_added
    assert not comparison.nodes_removed
    assert not comparison.nodes_changed
    assert not comparison.edges_added
    assert not comparison.edges_removed
    assert not comparison.edges_changed
    assert not comparison.behaviour_changes


def test_added_node_is_reported_with_target_provenance() -> None:
    base = _side(nodes=[_node("start", node_type="placeholder", label="Start", connector_type=None)])
    target = _side(
        contract=_contract(proposal_id=_TARGET_PROPOSAL_ID, confidence="high"),
        nodes=[
            _node("start", node_type="placeholder", label="Start", connector_type=None),
            _node("review", label="Code Review"),
        ],
    )

    comparison = compare_proposals(base, target)

    assert [entry.id for entry in comparison.nodes_added] == ["review"]
    added = comparison.nodes_added[0]
    assert added.side == "target"
    assert added.node.label == "Code Review"
    assert added.provenance.confidence == "high"


def test_removed_node_is_reported_with_base_provenance() -> None:
    base = _side(contract=_contract(confidence="low"), nodes=[_node(), _node("ci_cd", label="CI/CD Pipeline")])
    target = _side(nodes=[_node()])

    comparison = compare_proposals(base, target)

    assert [entry.id for entry in comparison.nodes_removed] == ["ci_cd"]
    removed = comparison.nodes_removed[0]
    assert removed.side == "base"
    assert removed.node.label == "CI/CD Pipeline"
    assert removed.provenance.confidence == "low"


def test_changed_node_names_the_property_and_both_values() -> None:
    base = _side(nodes=[_node(connector_type="github")])
    target = _side(nodes=[_node(connector_type="gitlab")])

    comparison = compare_proposals(base, target)

    assert len(comparison.nodes_changed) == 1
    changed = comparison.nodes_changed[0]
    assert changed.id == "development"
    connector_change = next(change for change in changed.changes if change.property == "connector_type")
    assert connector_change.base_value == "github"
    assert connector_change.target_value == "gitlab"


def test_changed_node_carries_both_sides_provenance() -> None:
    base = _side(contract=_contract(confidence="low"), nodes=[_node(label="Dev")])
    target = _side(
        contract=_contract(proposal_id=_TARGET_PROPOSAL_ID, confidence="high"),
        nodes=[_node(label="Development")],
    )

    comparison = compare_proposals(base, target)

    changed = comparison.nodes_changed[0]
    assert changed.base_provenance.confidence == "low"
    assert changed.target_provenance.confidence == "high"


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_added_edge_is_reported() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes)
    target = _side(nodes=nodes, edges=[_edge("start", "review")])

    comparison = compare_proposals(base, target)

    assert [entry.id for entry in comparison.edges_added] == ["start->review"]
    assert comparison.edges_added[0].side == "target"


def test_removed_edge_is_reported() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes, edges=[_edge("start", "review")])
    target = _side(nodes=nodes)

    comparison = compare_proposals(base, target)

    assert [entry.id for entry in comparison.edges_removed] == ["start->review"]
    assert comparison.edges_removed[0].side == "base"


def test_changed_edge_reports_the_edge_type_change() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes, edges=[_edge("start", "review")])
    target = _side(nodes=nodes, edges=[_edge("start", "review", edge_type="conditional")])

    comparison = compare_proposals(base, target)

    assert len(comparison.edges_changed) == 1
    change = comparison.edges_changed[0].changes[0]
    assert change.property == "edge_type"
    assert change.base_value == "normal"
    assert change.target_value == "conditional"


# ---------------------------------------------------------------------------
# Approval / HITL gates
# ---------------------------------------------------------------------------


def test_approval_gate_removed_from_existing_edge_is_reported() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes, edges=[_edge("start", "review", hitl_review=True)])
    target = _side(nodes=nodes, edges=[_edge("start", "review")])

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["approval_gate_removed"]
    change = comparison.behaviour_changes[0]
    assert change.subject_kind == "edge"
    assert change.subject_id == "start->review"
    assert change.property == "hitl_review"
    assert change.base_value is True
    assert change.target_value is False


def test_approval_gate_added_to_existing_edge_is_reported() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes, edges=[_edge("start", "review")])
    target = _side(nodes=nodes, edges=[_edge("start", "review", hitl_review=True)])

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["approval_gate_added"]
    assert comparison.behaviour_changes[0].base_value is False
    assert comparison.behaviour_changes[0].target_value is True


def test_removed_edge_with_approval_gate_reports_the_gate_as_removed() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes, edges=[_edge("start", "review", hitl_review=True)])
    target = _side(nodes=nodes)

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["approval_gate_removed"]
    change = comparison.behaviour_changes[0]
    assert change.base_value is True
    assert change.target_value is None


def test_added_edge_with_approval_gate_reports_the_gate_as_added() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes)
    target = _side(nodes=nodes, edges=[_edge("start", "review", hitl_review=True)])

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["approval_gate_added"]
    assert comparison.behaviour_changes[0].base_value is None
    assert comparison.behaviour_changes[0].target_value is True


def test_removed_edge_without_approval_gate_is_not_a_behaviour_change() -> None:
    nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("review")]
    base = _side(nodes=nodes, edges=[_edge("start", "review")])
    target = _side(nodes=nodes)

    comparison = compare_proposals(base, target)

    assert not comparison.behaviour_changes


# ---------------------------------------------------------------------------
# Automated vs manual steps
# ---------------------------------------------------------------------------


def test_automated_step_replaced_by_manual_step_is_reported() -> None:
    base = _side(nodes=[_node("review", node_type="agent", label="Code Review")])
    target = _side(nodes=[_node("review", node_type="manual", label="Code Review")], contract=_contract())

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["step_deautomated"]
    change = comparison.behaviour_changes[0]
    assert change.subject_kind == "node"
    assert change.subject_id == "review"
    assert change.base_value == "agent"
    assert change.target_value == "manual"


def test_manual_step_becoming_automated_is_reported() -> None:
    base = _side(nodes=[_node("planning", node_type="manual", label="Planning (Ticket Triage)")])
    target = _side(nodes=[_node("planning", node_type="agent", label="Planning (Ticket Triage)")])

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["step_automated"]
    assert comparison.behaviour_changes[0].base_value == "manual"
    assert comparison.behaviour_changes[0].target_value == "agent"


def test_added_manual_step_is_reported() -> None:
    base = _side(nodes=[_node("start", node_type="placeholder", label="Start", connector_type=None)])
    target = _side(
        nodes=[
            _node("start", node_type="placeholder", label="Start", connector_type=None),
            _node("planning", node_type="manual", label="Planning (Ticket Triage)", connector_type=None),
        ]
    )

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["manual_step_added"]
    assert comparison.behaviour_changes[0].subject_id == "planning"
    assert comparison.behaviour_changes[0].target_value == "manual"


def test_removed_manual_step_is_reported() -> None:
    base = _side(
        nodes=[
            _node("start", node_type="placeholder", label="Start", connector_type=None),
            _node("planning", node_type="manual", label="Planning (Ticket Triage)", connector_type=None),
        ]
    )
    target = _side(nodes=[_node("start", node_type="placeholder", label="Start", connector_type=None)])

    comparison = compare_proposals(base, target)

    assert _behaviour_kinds(comparison) == ["manual_step_removed"]
    assert comparison.behaviour_changes[0].base_value == "manual"


def test_placeholder_step_change_is_not_reported_as_automation() -> None:
    base = _side(nodes=[_node("start", node_type="placeholder", label="Start", connector_type=None)])
    target = _side(nodes=[_node("start", node_type="agent", label="Start", connector_type=None)])

    comparison = compare_proposals(base, target)

    assert not comparison.behaviour_changes


def test_label_change_is_not_a_behaviour_change() -> None:
    base = _side(nodes=[_node("review", node_type="agent", label="Code Review")])
    target = _side(nodes=[_node("review", node_type="agent", label="Automated Code Review")])

    comparison = compare_proposals(base, target)

    assert len(comparison.nodes_changed) == 1
    assert not comparison.behaviour_changes


# ---------------------------------------------------------------------------
# Provenance, empty inputs, determinism
# ---------------------------------------------------------------------------


def test_provenance_evidence_confidence_and_uncertainty_survive_on_both_sides() -> None:
    base_contract = _contract(
        confidence="low",
        uncertainty=["Issue history was not available"],
        provenance=[_evidence("evidence:base", kind="observed")],
    )
    target_contract = _contract(
        proposal_id=_TARGET_PROPOSAL_ID,
        confidence="high",
        uncertainty=["Deployment cadence is unconfirmed"],
        provenance=[_evidence("evidence:target", kind="inferred")],
        unavailable=[
            UnavailableEvidence(
                source_id="jira-1:issues",
                requested_resource="issues",
                reason="403 from Jira",
            )
        ],
    )

    comparison = compare_proposals(
        _side(contract=base_contract),
        _side(contract=target_contract, nodes=[_node("review")]),
    )

    assert comparison.base.confidence == "low"
    assert comparison.base.uncertainty == ["Issue history was not available"]
    assert [reference.reference_id for reference in comparison.base.provenance] == ["evidence:base"]
    assert comparison.target.confidence == "high"
    assert comparison.target.uncertainty == ["Deployment cadence is unconfirmed"]
    assert [reference.reference_id for reference in comparison.target.provenance] == ["evidence:target"]
    assert [item.source_id for item in comparison.target.unavailable_evidence] == ["jira-1:issues"]

    added = comparison.nodes_added[0]
    assert added.provenance.provenance_kinds == ["inferred"]
    assert [reference.reference_id for reference in added.provenance.evidence_refs] == ["evidence:target"]
    assert added.provenance.confidence == "high"
    assert added.provenance.uncertainty == ["Deployment cadence is unconfirmed"]


def test_provenance_kinds_are_deduplicated_in_first_appearance_order() -> None:
    target_contract = _contract(
        proposal_id=_TARGET_PROPOSAL_ID,
        provenance=[
            _evidence("evidence:a", kind="observed"),
            _evidence("evidence:b", kind="observed"),
            _evidence("evidence:c", kind="inferred"),
        ],
    )

    comparison = compare_proposals(
        _side(),
        _side(contract=target_contract, nodes=[_node("review")]),
    )

    added = comparison.nodes_added[0]
    assert added.provenance.provenance_kinds == ["observed", "inferred"]
    assert [reference.reference_id for reference in added.provenance.evidence_refs] == [
        "evidence:a",
        "evidence:b",
        "evidence:c",
    ]


def test_empty_proposals_compare_to_no_changes() -> None:
    empty = _contract(observed=[], derived=[], provenance=[], uncertainty=[])
    comparison = compare_proposals(_side(contract=empty), _side(contract=empty))

    assert not comparison.nodes_added
    assert not comparison.nodes_removed
    assert not comparison.nodes_changed
    assert not comparison.edges_added
    assert not comparison.edges_removed
    assert not comparison.edges_changed
    assert not comparison.behaviour_changes
    assert not comparison.base.provenance
    assert comparison.contract_version == "alpha.determination-comparison.v1"


def test_side_summaries_keep_the_two_proposals_distinct() -> None:
    base_contract = _contract(review_state="approved")
    target_contract = _contract(proposal_id=_TARGET_PROPOSAL_ID, review_state="needs_review", confidence="high")

    comparison = compare_proposals(_side(contract=base_contract), _side(contract=target_contract))

    assert comparison.base.side == "base"
    assert comparison.base.proposal_id == _PROPOSAL_ID
    assert comparison.base.review_state == "approved"
    assert comparison.target.side == "target"
    assert comparison.target.proposal_id == _TARGET_PROPOSAL_ID
    assert comparison.target.review_state == "needs_review"
    assert comparison.base.activation_status == "not_activated"
    assert comparison.target.activation_status == "not_activated"


def test_result_is_deterministic_and_inputs_are_not_mutated() -> None:
    base = _side(
        nodes=[
            _node("start", node_type="placeholder", label="Start", connector_type=None),
            _node("b"),
            _node("shared"),
        ],
        edges=[_edge("start", "shared")],
    )
    target = _side(
        nodes=[
            _node("a"),
            _node("shared", label="Shared (renamed)"),
            _node("end", node_type="placeholder", label="End", connector_type=None),
        ],
        edges=[_edge("shared", "end")],
    )
    base_before = base.model_dump(mode="json")
    target_before = target.model_dump(mode="json")

    first = compare_proposals(base, target)
    second = compare_proposals(base, target)

    assert first.model_dump() == second.model_dump()
    assert base.model_dump(mode="json") == base_before
    assert target.model_dump(mode="json") == target_before


def test_result_does_not_depend_on_input_order() -> None:
    base_nodes = [_node("start", node_type="placeholder", label="Start", connector_type=None), _node("b"), _node("a")]
    base_edges = [_edge("start", "b"), _edge("start", "a")]
    target_nodes = [_node("c"), _node("a")]

    forward = compare_proposals(
        _side(nodes=base_nodes, edges=base_edges),
        _side(nodes=target_nodes),
    )
    reversed_input = compare_proposals(
        _side(nodes=list(reversed(base_nodes)), edges=list(reversed(base_edges))),
        _side(nodes=list(reversed(target_nodes))),
    )

    assert forward.model_dump() == reversed_input.model_dump()


# ---------------------------------------------------------------------------
# Input integrity
# ---------------------------------------------------------------------------


def test_duplicate_node_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate node ids"):
        _side(nodes=[_node("development"), _node("development", label="Other")])


def test_duplicate_edges_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate edges"):
        _side(
            nodes=[_node("a"), _node("b")],
            edges=[_edge("a", "b"), _edge("a", "b", edge_type="conditional")],
        )


def test_edge_referencing_unknown_node_is_rejected() -> None:
    with pytest.raises(ValidationError, match="absent from this side"):
        _side(nodes=[_node("a")], edges=[_edge("a", "ghost")])
