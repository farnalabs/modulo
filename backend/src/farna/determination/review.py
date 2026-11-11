"""Review-decision validation for determination proposals.

Pure and side-effect free: a reviewer's decision is validated against a
``ProposalContract``, optional corrections are applied to derived elements,
and the resulting contract plus the effective review state is returned. Nothing
here persists, activates or touches a pipeline - the route records the decision
in the audit chain and the contract's ``activation_status`` is always
``not_activated``.

Approval is refused while the proposal carries unresolved assumptions whose
confidence is ``low`` or ``unknown`` unless the reviewer acknowledges each one
by id; the acknowledgement ids travel with the decision so the audit record can
carry them verbatim.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from farna.determination.models import (
    ProposalConfidence,
    ProposalContract,
    ProposalFact,
    ReviewState,
    UnresolvedAssumption,
)

ReviewDecision = Literal["approve", "reject", "needs_review"]

#: Assumption confidences that block approval until acknowledged by id.
_UNSETTLED_CONFIDENCE: frozenset[ProposalConfidence] = frozenset({"unknown", "low"})

#: The review state a decision leaves the proposal in.
_DECISION_REVIEW_STATE: dict[ReviewDecision, ReviewState] = {
    "approve": "approved",
    "reject": "rejected",
    "needs_review": "needs_review",
}


class ProposalReviewError(ValueError):
    """A decision the proposal contract refuses to accept.

    ``blocking_assumption_ids`` is populated only for the approval-blocked-by-
    unacknowledged-assumptions case so the route can return the exact ids the
    reviewer must acknowledge.
    """

    def __init__(
        self,
        message: str,
        *,
        blocking_assumption_ids: list[uuid.UUID] | None = None,
    ) -> None:
        super().__init__(message)
        self.blocking_assumption_ids: list[uuid.UUID] = list(blocking_assumption_ids or [])


class DerivedElementEdit(BaseModel):
    """A correction to one derived element of a proposal.

    At least one of ``statement`` / ``confidence`` must be supplied; a no-op
    correction is rejected rather than silently accepted.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    statement: str | None = Field(default=None, min_length=1)
    confidence: ProposalConfidence | None = None

    @model_validator(mode="after")
    def changes_something(self) -> DerivedElementEdit:
        if self.statement is None and self.confidence is None:
            raise ValueError("a correction must change the statement or the confidence")
        return self


class ProposalReviewRequest(BaseModel):
    """A reviewer's decision on a determination proposal."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: uuid.UUID
    proposal: ProposalContract
    decision: ReviewDecision
    reason: str | None = Field(default=None, max_length=2000)
    corrected_derived_elements: list[DerivedElementEdit] = Field(default_factory=list)
    removed_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)
    acknowledged_assumption_ids: list[uuid.UUID] = Field(default_factory=list)


class ReviewedProposal(BaseModel):
    """The validated outcome of one review decision."""

    model_config = ConfigDict(extra="forbid")

    proposal: ProposalContract
    decision: ReviewDecision
    review_state: ReviewState
    activation_status: Literal["not_activated"] = "not_activated"
    decided_at: datetime
    reason: str | None = None
    acknowledged_assumption_ids: list[uuid.UUID] = Field(default_factory=list)
    corrected_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)
    removed_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)


def review_proposal(
    review_request: ProposalReviewRequest,
    *,
    decided_at: datetime | None = None,
) -> ReviewedProposal:
    """Validate *review_request* and return the reviewed proposal contract.

    Raises ``ProposalReviewError`` (a ``ValueError``) when the decision cannot
    be recorded: a reject without a reason, a proposal id mismatch, corrections
    that do not target the proposal's derived elements, acknowledgements that
    name assumptions the proposal does not carry, or an approval that leaves
    low/unknown-confidence assumptions unacknowledged.

    The returned contract has its ``review_state`` set from the decision and
    ``activation_status`` pinned to ``not_activated`` - reviewing a proposal
    never creates a pipeline, enables a trigger or writes to a connected tool.
    """
    if review_request.proposal_id != review_request.proposal.proposal_id:
        raise ProposalReviewError("proposal_id does not match the proposal contract")

    reason = _normalised_reason(review_request.reason)
    if review_request.decision == "reject" and reason is None:
        raise ProposalReviewError("a reject decision requires a reason")

    corrected = _apply_corrections(
        review_request.proposal,
        review_request.corrected_derived_elements,
        review_request.removed_derived_element_ids,
    )
    acknowledged = _validated_acknowledgements(
        corrected.unresolved_assumptions,
        review_request.acknowledged_assumption_ids,
    )

    if review_request.decision == "approve":
        acknowledged_ids = set(acknowledged)
        blocking = _blocking_assumption_ids(corrected.unresolved_assumptions, acknowledged_ids)
        if blocking:
            raise ProposalReviewError(
                "approval is blocked by unacknowledged low or unknown confidence assumptions",
                blocking_assumption_ids=blocking,
            )

    review_state = _DECISION_REVIEW_STATE[review_request.decision]
    reviewed_contract = ProposalContract.model_validate(corrected.model_copy(update={"review_state": review_state, "activation_status": "not_activated"}).model_dump())

    return ReviewedProposal(
        proposal=reviewed_contract,
        decision=review_request.decision,
        review_state=review_state,
        activation_status="not_activated",
        decided_at=decided_at if decided_at is not None else datetime.now(UTC),
        reason=reason,
        acknowledged_assumption_ids=acknowledged,
        corrected_derived_element_ids=[edit.id for edit in review_request.corrected_derived_elements],
        removed_derived_element_ids=list(review_request.removed_derived_element_ids),
    )


def _normalised_reason(reason: str | None) -> str | None:
    """Trim *reason*, treating an all-whitespace value as absent."""
    if reason is None:
        return None
    stripped = reason.strip()
    return stripped or None


def _blocking_assumption_ids(
    assumptions: list[UnresolvedAssumption],
    acknowledged_ids: set[uuid.UUID],
) -> list[uuid.UUID]:
    """Assumption ids that must be acknowledged before approval."""
    return [assumption.id for assumption in assumptions if assumption.confidence in _UNSETTLED_CONFIDENCE and assumption.id not in acknowledged_ids]


def _validated_acknowledgements(
    assumptions: list[UnresolvedAssumption],
    acknowledged_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Return the acknowledged ids in order, refusing ids the proposal does not carry."""
    known = {assumption.id for assumption in assumptions}
    unknown = [assumption_id for assumption_id in acknowledged_ids if assumption_id not in known]
    if unknown:
        raise ProposalReviewError("acknowledged assumption ids do not belong to this proposal")
    return list(dict.fromkeys(acknowledged_ids))


def _apply_corrections(
    proposal: ProposalContract,
    edits: list[DerivedElementEdit],
    removed_ids: list[uuid.UUID],
) -> ProposalContract:
    """Apply derived-element corrections, revalidating the resulting contract.

    Only derived elements may be corrected: an edit or removal naming an
    observed fact (or any id the proposal does not carry) is refused rather
    than silently ignored.
    """
    if not edits and not removed_ids:
        return proposal

    derived_by_id = {fact.id: fact for fact in proposal.derived_elements}
    edit_ids = [edit.id for edit in edits]
    if len(edit_ids) != len(set(edit_ids)):
        raise ProposalReviewError("duplicate corrections for the same derived element")
    if len(removed_ids) != len(set(removed_ids)):
        raise ProposalReviewError("duplicate removals for the same derived element")
    if set(edit_ids) & set(removed_ids):
        raise ProposalReviewError("a derived element cannot be corrected and removed in the same decision")

    unknown = [element_id for element_id in [*edit_ids, *removed_ids] if element_id not in derived_by_id]
    if unknown:
        raise ProposalReviewError("corrected or removed ids are not derived elements of this proposal")

    edits_by_id = {edit.id: edit for edit in edits}
    updated: list[ProposalFact] = []
    for fact in proposal.derived_elements:
        if fact.id in removed_ids:
            continue
        edit = edits_by_id.get(fact.id)
        updated.append(fact if edit is None else _corrected_fact(fact, edit))

    corrected = proposal.model_copy(update={"derived_elements": updated})
    # Revalidate through the contract so an edit can never produce an
    # unparseable proposal (e.g. a non-derived fact in derived_elements).
    return ProposalContract.model_validate(corrected.model_dump())


def _corrected_fact(fact: ProposalFact, edit: DerivedElementEdit) -> ProposalFact:
    """Return *fact* with the fields the edit supplies replaced."""
    updates: dict[str, object] = {}
    if edit.statement is not None:
        updates["statement"] = edit.statement
    if edit.confidence is not None:
        updates["confidence"] = edit.confidence
    return fact.model_copy(update=updates)
