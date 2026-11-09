"""Tests for the Alpha determination proposal contract."""

import uuid

import pytest
from pydantic import ValidationError

from modulo.connectors.base import ConnectorType
from modulo.determination.inference import Finding, build_proposal_contract
from modulo.determination.models import ProposalContract, SourceInventoryItem

from .helpers import make_sample


def test_valid_contract_is_versioned_evidence_backed_and_not_activated() -> None:
    sample = make_sample("repos", [{"name": "repo"}], connector_type=ConnectorType.GITHUB)
    contract = build_proposal_contract(
        [sample],
        [Finding(category="stage", finding="Development observed", evidence="repo", confidence="high")],
    )

    assert contract.contract_version == "alpha.determination-proposal.v1"
    assert contract.activation_status == "not_activated"
    assert contract.review_state == "needs_review"
    assert contract.observed_facts[0].kind == "observed"
    assert contract.derived_elements[0].kind == "derived"
    assert contract.provenance[0].provenance == "observed"
    assert contract.attempt.source_inventory[0].sample_count == 1


def test_derived_element_links_observed_evidence_for_its_connector() -> None:
    sample = make_sample("repos", [{"name": "repo"}], connector_type=ConnectorType.GITHUB)
    contract = build_proposal_contract(
        [sample],
        [
            Finding(
                category="stage",
                finding="Development observed",
                evidence="1 repository accessible",
                confidence="high",
                related_connector=sample.connector_id,
            )
        ],
    )

    derived = contract.derived_elements[0]
    assert derived.evidence_refs
    assert derived.evidence_refs[0].source_id == f"{sample.connector_id}:repos"
    assert derived.evidence_refs == contract.provenance


def test_inferred_derived_elements_carry_their_connector_evidence() -> None:
    sample = make_sample("repos", [{"name": "repo"}], connector_type=ConnectorType.GITHUB)

    contract = build_proposal_contract([sample])

    development = next(item for item in contract.derived_elements if "Development" in item.statement)
    assert development.evidence_refs
    assert all(ref.source_id == f"{sample.connector_id}:repos" for ref in development.evidence_refs)


def test_ambiguous_connector_attribution_leaves_derived_evidence_unlinked() -> None:
    github = make_sample("repos", [{"name": "repo"}], connector_type=ConnectorType.GITHUB)
    gitlab = make_sample("repos", [{"name": "repo"}], connector_type=ConnectorType.GITLAB)
    contract = build_proposal_contract([github, gitlab])

    development = next(item for item in contract.derived_elements if "Development" in item.statement)
    assert not development.evidence_refs


def test_inventory_rejects_error_on_available_source() -> None:
    with pytest.raises(ValidationError, match="unavailable"):
        SourceInventoryItem(
            source_id="source",
            connector_type="github",
            resource="repos",
            sample_count=1,
            status="available",
            error="boom",
        )


def test_incomplete_sample_surfaces_unavailable_evidence_and_assumption() -> None:
    sample = make_sample("issues", [], connector_type=ConnectorType.JIRA, error="permission denied")
    contract = build_proposal_contract(
        [sample],
        [
            Finding(
                category="overview",
                finding="Planning may exist",
                evidence="sample unavailable",
                confidence="low",
                uncertainty="Issue history was not available",
            )
        ],
    )

    assert contract.attempt.status == "completed_with_gaps"
    assert contract.unavailable_evidence[0].reason == "permission denied"
    assert contract.unresolved_assumptions[0].statement == "Issue history was not available"
    assert contract.confidence == "low"


def test_empty_records_sample_is_available_but_produces_no_observed_fact() -> None:
    sample = make_sample("issues", [], connector_type=ConnectorType.JIRA)

    contract = build_proposal_contract([sample], [])

    assert contract.attempt.source_inventory[0].status == "available"
    assert not contract.observed_facts
    assert not contract.provenance
    assert contract.attempt.status == "completed"


def test_inventory_rejects_empty_error_string() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        SourceInventoryItem(
            source_id="source",
            connector_type="github",
            resource="repos",
            sample_count=0,
            status="unavailable",
            error="",
        )


def test_empty_determination_uses_explicit_unavailable_inventory() -> None:
    contract = build_proposal_contract([])

    assert contract.attempt.source_inventory[0].source_id == "determination:none"
    assert contract.attempt.status == "completed_with_gaps"
    assert contract.confidence == "low"
    assert contract.activation_status == "not_activated"


def test_contract_rejects_invalid_provenance_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ProposalContract(
            proposal_id=uuid.uuid4(),
            attempt={
                "id": uuid.uuid4(),
                "status": "completed",
                "source_inventory": [
                    {
                        "source_id": "source",
                        "connector_type": "github",
                        "resource": "repos",
                        "sample_count": 1,
                        "status": "available",
                    }
                ],
            },
            confidence="high",
            provenance=[
                {
                    "reference_id": "ref",
                    "source_id": "source",
                    "locator": "scan:repos",
                    "provenance": "claimed",
                }
            ],
            unexpected="not accepted",
        )


def test_contract_rejects_observed_fact_that_is_derived() -> None:
    with pytest.raises(ValidationError, match="observed_facts"):
        ProposalContract(
            proposal_id=uuid.uuid4(),
            attempt={
                "id": uuid.uuid4(),
                "status": "completed",
                "source_inventory": [
                    {
                        "source_id": "source",
                        "connector_type": "github",
                        "resource": "repos",
                        "sample_count": 1,
                        "status": "available",
                    }
                ],
            },
            confidence="high",
            observed_facts=[
                {
                    "id": uuid.uuid4(),
                    "statement": "derived",
                    "kind": "derived",
                    "confidence": "high",
                }
            ],
        )


def test_contract_rejects_derived_element_that_is_observed() -> None:
    with pytest.raises(ValidationError, match="derived_elements"):
        ProposalContract(
            proposal_id=uuid.uuid4(),
            attempt={
                "id": uuid.uuid4(),
                "status": "completed",
                "source_inventory": [
                    {
                        "source_id": "source",
                        "connector_type": "github",
                        "resource": "repos",
                        "sample_count": 1,
                        "status": "available",
                    }
                ],
            },
            confidence="high",
            derived_elements=[
                {
                    "id": uuid.uuid4(),
                    "statement": "observed",
                    "kind": "observed",
                    "confidence": "high",
                }
            ],
        )
