"""Tests for the Alpha determination proposal contract."""

import uuid

import pytest
from pydantic import ValidationError

from modulo.connectors.base import ConnectorType
from modulo.determination.inference import Finding, build_proposal_contract
from modulo.determination.models import ProposalContract

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
