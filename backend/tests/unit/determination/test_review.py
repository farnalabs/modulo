"""Tests for determination proposal review-decision validation.

The validator is pure: these tests pin the decision semantics (approve /
reject / needs_review), the approval gate on low/unknown-confidence
assumptions, derived-element corrections, and the guarantee that reviewing a
proposal never activates it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from farna.determination.models import (
    DeterminationAttempt,
    ProposalContract,
    ProposalFact,
    SourceInventoryItem,
    UnresolvedAssumption,
)
from farna.determination.review import (
    DerivedElementEdit,
    ProposalReviewError,
    ProposalReviewRequest,
    ReviewDecision,
    review_proposal,
)

_PROPOSAL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ATTEMPT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OBSERVED_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_DERIVED_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ASSUMPTION_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_DECIDED_AT = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def make_contract(
    *,
    assumptions: list[UnresolvedAssumption] | None = None,
    derived_statement: str = "Development stage observed",
) -> ProposalContract:
    return ProposalContract(
        proposal_id=_PROPOSAL_ID,
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
        observed_facts=[
            ProposalFact(
                id=_OBSERVED_ID,
                statement="Observed 1 repository",
                kind="observed",
                confidence="high",
            )
        ],
        derived_elements=[
            ProposalFact(
                id=_DERIVED_ID,
                statement=derived_statement,
                kind="derived",
                confidence="medium",
            )
        ],
        unresolved_assumptions=assumptions
        if assumptions is not None
        else [
            UnresolvedAssumption(
                id=_ASSUMPTION_ID,
                statement="Deployment cadence is unknown",
                reason="No CI/CD evidence was sampled",
                confidence="low",
            )
        ],
        confidence="medium",
    )


def make_request(
    contract: ProposalContract | None = None,
    *,
    decision: ReviewDecision = "approve",
    reason: str | None = None,
    corrected_derived_elements: list[DerivedElementEdit] | None = None,
    removed_derived_element_ids: list[uuid.UUID] | None = None,
    acknowledged_assumption_ids: list[uuid.UUID] | None = None,
) -> ProposalReviewRequest:
    resolved = contract if contract is not None else make_contract()
    return ProposalReviewRequest(
        proposal_id=resolved.proposal_id,
        proposal=resolved,
        decision=decision,
        reason=reason,
        corrected_derived_elements=corrected_derived_elements or [],
        removed_derived_element_ids=removed_derived_element_ids or [],
        acknowledged_assumption_ids=acknowledged_assumption_ids or [],
    )


def test_approve_sets_approved_and_stays_not_activated() -> None:
    reviewed = review_proposal(
        make_request(acknowledged_assumption_ids=[_ASSUMPTION_ID]),
        decided_at=_DECIDED_AT,
    )

    assert reviewed.decision == "approve"
    assert reviewed.review_state == "approved"
    assert reviewed.activation_status == "not_activated"
    assert reviewed.proposal.review_state == "approved"
    assert reviewed.proposal.activation_status == "not_activated"
    assert reviewed.decided_at == _DECIDED_AT
    assert reviewed.acknowledged_assumption_ids == [_ASSUMPTION_ID]


def test_approve_blocked_by_unacknowledged_low_confidence_assumption() -> None:
    with pytest.raises(ProposalReviewError) as excinfo:
        review_proposal(make_request())

    assert excinfo.value.blocking_assumption_ids == [_ASSUMPTION_ID]
    assert "unacknowledged" in str(excinfo.value)


def test_approve_blocked_by_unacknowledged_unknown_confidence_assumption() -> None:
    contract = make_contract(
        assumptions=[
            UnresolvedAssumption(
                id=_ASSUMPTION_ID,
                statement="Ownership is unclear",
                reason="No CODEOWNERS evidence",
                confidence="unknown",
            )
        ]
    )

    with pytest.raises(ProposalReviewError) as excinfo:
        review_proposal(make_request(contract))

    assert excinfo.value.blocking_assumption_ids == [_ASSUMPTION_ID]


def test_approve_is_not_blocked_by_high_confidence_assumption() -> None:
    contract = make_contract(
        assumptions=[
            UnresolvedAssumption(
                id=_ASSUMPTION_ID,
                statement="Deployment cadence is weekly",
                reason="Sampled CI schedule",
                confidence="high",
            )
        ]
    )

    reviewed = review_proposal(make_request(contract))

    assert reviewed.review_state == "approved"
    assert not reviewed.acknowledged_assumption_ids


def test_acknowledgement_permits_approval() -> None:
    reviewed = review_proposal(
        make_request(acknowledged_assumption_ids=[_ASSUMPTION_ID]),
    )

    assert reviewed.review_state == "approved"


def test_reject_requires_a_reason() -> None:
    with pytest.raises(ProposalReviewError, match="reason"):
        review_proposal(make_request(decision="reject"))


def test_reject_with_whitespace_reason_is_refused() -> None:
    with pytest.raises(ProposalReviewError, match="reason"):
        review_proposal(make_request(decision="reject", reason="   "))


def test_reject_records_the_reason_and_rejected_state() -> None:
    reviewed = review_proposal(
        make_request(decision="reject", reason="Evidence covers repositories only"),
    )

    assert reviewed.review_state == "rejected"
    assert reviewed.proposal.review_state == "rejected"
    assert reviewed.reason == "Evidence covers repositories only"
    assert reviewed.activation_status == "not_activated"


def test_needs_review_records_needs_review_state() -> None:
    reviewed = review_proposal(make_request(decision="needs_review"))

    assert reviewed.review_state == "needs_review"
    assert reviewed.proposal.review_state == "needs_review"
    assert reviewed.activation_status == "not_activated"


def test_editing_a_derived_element_produces_a_valid_contract() -> None:
    reviewed = review_proposal(
        make_request(
            corrected_derived_elements=[
                DerivedElementEdit(id=_DERIVED_ID, statement="Delivery stage observed", confidence="high")
            ],
            acknowledged_assumption_ids=[_ASSUMPTION_ID],
        ),
    )

    derived = reviewed.proposal.derived_elements[0]
    assert derived.statement == "Delivery stage observed"
    assert derived.confidence == "high"
    assert derived.kind == "derived"
    # The corrected contract parses as a full ProposalContract.
    assert ProposalContract.model_validate(reviewed.proposal.model_dump()).derived_elements[0].statement == (
        "Delivery stage observed"
    )
    assert reviewed.corrected_derived_element_ids == [_DERIVED_ID]


def test_removing_a_derived_element_produces_a_valid_contract() -> None:
    reviewed = review_proposal(
        make_request(
            removed_derived_element_ids=[_DERIVED_ID],
            acknowledged_assumption_ids=[_ASSUMPTION_ID],
        ),
    )

    assert not reviewed.proposal.derived_elements
    assert reviewed.removed_derived_element_ids == [_DERIVED_ID]


def test_correction_of_an_observed_fact_is_refused() -> None:
    with pytest.raises(ProposalReviewError, match="derived elements"):
        review_proposal(
            make_request(corrected_derived_elements=[DerivedElementEdit(id=_OBSERVED_ID, statement="Rewritten")])
        )


def test_removal_of_an_unknown_id_is_refused() -> None:
    with pytest.raises(ProposalReviewError, match="derived elements"):
        review_proposal(make_request(removed_derived_element_ids=[uuid.uuid4()]))


def test_correcting_and_removing_the_same_element_is_refused() -> None:
    with pytest.raises(ProposalReviewError, match="corrected and removed"):
        review_proposal(
            make_request(
                corrected_derived_elements=[DerivedElementEdit(id=_DERIVED_ID, statement="Changed")],
                removed_derived_element_ids=[_DERIVED_ID],
            )
        )


def test_duplicate_corrections_are_refused() -> None:
    with pytest.raises(ProposalReviewError, match="duplicate corrections"):
        review_proposal(
            make_request(
                corrected_derived_elements=[
                    DerivedElementEdit(id=_DERIVED_ID, statement="One"),
                    DerivedElementEdit(id=_DERIVED_ID, statement="Two"),
                ]
            )
        )


def test_duplicate_removals_are_refused() -> None:
    with pytest.raises(ProposalReviewError, match="duplicate removals"):
        review_proposal(make_request(removed_derived_element_ids=[_DERIVED_ID, _DERIVED_ID]))


def test_confidence_only_correction_keeps_the_statement() -> None:
    reviewed = review_proposal(
        make_request(
            decision="needs_review",
            corrected_derived_elements=[DerivedElementEdit(id=_DERIVED_ID, confidence="high")],
        )
    )

    derived = reviewed.proposal.derived_elements[0]
    assert derived.confidence == "high"
    assert derived.statement == "Development stage observed"


def test_acknowledging_an_unknown_assumption_is_refused() -> None:
    with pytest.raises(ProposalReviewError, match="assumption ids"):
        review_proposal(make_request(acknowledged_assumption_ids=[uuid.uuid4()]))


def test_proposal_id_mismatch_is_refused() -> None:
    contract = make_contract()
    request = ProposalReviewRequest(
        proposal_id=uuid.uuid4(),
        proposal=contract,
        decision="approve",
    )

    with pytest.raises(ProposalReviewError, match="proposal_id"):
        review_proposal(request)


def test_noop_correction_is_refused() -> None:
    with pytest.raises(ValidationError, match="statement or the confidence"):
        DerivedElementEdit(id=_DERIVED_ID)


def test_unknown_decision_value_is_refused() -> None:
    with pytest.raises(ValidationError):
        ProposalReviewRequest(
            proposal_id=_PROPOSAL_ID,
            proposal=make_contract(),
            decision="maybe",  # type: ignore[arg-type]
        )


def test_review_does_not_mutate_the_input_contract() -> None:
    contract = make_contract()

    reviewed = review_proposal(
        make_request(
            contract,
            corrected_derived_elements=[DerivedElementEdit(id=_DERIVED_ID, statement="Changed")],
            acknowledged_assumption_ids=[_ASSUMPTION_ID],
        ),
    )

    assert contract.review_state == "needs_review"
    assert contract.derived_elements[0].statement == "Development stage observed"
    assert reviewed.proposal.derived_elements[0].statement == "Changed"


def test_acknowledgements_are_deduplicated_in_order() -> None:
    reviewed = review_proposal(
        make_request(acknowledged_assumption_ids=[_ASSUMPTION_ID, _ASSUMPTION_ID]),
    )

    assert reviewed.acknowledged_assumption_ids == [_ASSUMPTION_ID]
