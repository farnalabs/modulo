"""Rerun a determination and compare the fresh attempt with a prior attempt.

A determination proposal is never persisted: the "prior attempt" reaches a
rerun as client-supplied input, exactly like the proposal passed to
``POST /api/v1/determination/review``. :func:`rerun_proposal` is therefore pure
- it takes the prior contract and the freshly built contract, applies the
caller's carried-forward corrections to a copy of each, and returns a
:class:`RerunResult`. Nothing here activates, publishes or persists anything.

Classification never conflates "not reproduced" with "removed"
---------------------------------------------------------------
A prior inference the fresh attempt does not reproduce is ``not_reproduced``
when the sampled inputs are unchanged, and ``removed`` only when the inputs
genuinely changed (a source appeared, disappeared, flipped availability or
changed its sample count). Retained, new, confidence-changed and
evidence-changed elements are reported as distinct statuses.

Provenance is carried, never flattened
--------------------------------------
Both attempts' attempt id, status, confidence, uncertainty, evidence
references, unavailable evidence and source inventory are copied into the
result, so the prior (observed) side can never be read as the fresh side.

Relationship to ``comparison.py``
---------------------------------
:mod:`modulo.determination.comparison` compares the *draft graph* of two
proposals - ``compare_proposals`` over two :class:`~modulo.determination.comparison.ProposalComparisonSide`
values, reporting added/removed/changed nodes and edges, changed properties and
approval/automation behaviour. This module compares two *attempts' inferences*
(derived elements) and the sampled *inputs* that produced them, which that
contract has no representation for: a rerun produces no draft nodes or edges to
diff, and a comparison has no per-inference status (``not_reproduced`` /
``removed`` and friends) or source-inventory classification. The two therefore
remain separate modules with separate versioned contracts
(``alpha.determination-rerun.v1`` vs ``alpha.determination-comparison.v1``) and
no shared DTO: :class:`RerunAttemptSummary` carries the attempt status and
source inventory a rerun must show, while
:class:`~modulo.determination.comparison.ProposalSideSummary` carries the review
state, ``activation_status`` and ``side`` a comparison must show - neither shape
is a subset of the other. No helper is forked: both correction application
(``review._apply_corrections``) and per-fact correction
(``review._corrected_fact``) are imported from :mod:`modulo.determination.review`,
so a rerun and a review can never disagree about which corrections are valid.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from farna.determination.models import (
    EvidenceReference,
    ProposalConfidence,
    ProposalContract,
    ProposalFact,
    SourceInventoryItem,
    UnavailableEvidence,
)
from farna.determination.review import (
    DerivedElementEdit,
    ProposalReviewError,
    _apply_corrections,
    _corrected_fact,
)

RerunContractVersion = Literal["alpha.determination-rerun.v1"]

RERUN_CONTRACT_VERSION: RerunContractVersion = "alpha.determination-rerun.v1"

#: How one prior inference relates to the fresh attempt.
#:
#: ``confidence_changed`` takes precedence over ``evidence_changed`` when both
#: changed; both sides' values are carried on the item, so evidence movement is
#: still visible rather than lost.
ElementChangeStatus = Literal[
    "retained",
    "new",
    "not_reproduced",
    "removed",
    "confidence_changed",
    "evidence_changed",
]


class RerunError(ValueError):
    """An input a rerun refuses rather than silently ignoring."""


class ProposalRerunRequest(BaseModel):
    """A request to rerun determination against a prior attempt.

    ``prior_proposal`` is the attempt the caller last saw (never mutated). The
    corrections are the ones applied to that attempt: they are carried forward
    onto the fresh attempt by id, and recorded even when the fresh attempt no
    longer reproduces the element they targeted.
    """

    model_config = ConfigDict(extra="forbid")

    prior_proposal: ProposalContract
    corrected_derived_elements: list[DerivedElementEdit] = Field(default_factory=list)
    removed_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)


class CarriedForwardCorrections(BaseModel):
    """The prior attempt's corrections and whether the fresh attempt could take them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corrected_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)
    removed_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)
    unmatched_corrected_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)
    unmatched_removed_derived_element_ids: list[uuid.UUID] = Field(default_factory=list)


class SourceSampleCountChange(BaseModel):
    """A source that is still sampled but produced a different number of records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    prior_sample_count: int = Field(ge=0)
    current_sample_count: int = Field(ge=0)


class SourceInventoryChanges(BaseModel):
    """What changed in the sampled sources since the prior attempt.

    ``new_sources`` / ``missing_sources`` are sources that appear only on one
    side of the inventory; ``newly_available`` / ``newly_unavailable`` are
    sources present on both whose availability flipped. A missing source is
    surfaced explicitly rather than dropped: an inventory item the current scan
    did not produce is still an input change the caller must see.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    new_sources: list[SourceInventoryItem] = Field(default_factory=list)
    missing_sources: list[SourceInventoryItem] = Field(default_factory=list)
    newly_available: list[str] = Field(default_factory=list)
    newly_unavailable: list[str] = Field(default_factory=list)
    sample_count_changes: list[SourceSampleCountChange] = Field(default_factory=list)


class ElementChange(BaseModel):
    """One inference's relationship between the prior and the fresh attempt.

    ``statement`` is the element's statement on the side it exists (the current
    side for ``new`` and the changed statuses, the prior side for
    ``not_reproduced`` / ``removed``). Both sides' ids, confidences and evidence
    references are carried so a change can be inspected without the two
    contracts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ElementChangeStatus
    statement: str = Field(min_length=1)
    prior_element_id: uuid.UUID | None = None
    current_element_id: uuid.UUID | None = None
    prior_confidence: ProposalConfidence | None = None
    current_confidence: ProposalConfidence | None = None
    prior_evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    current_evidence_refs: list[EvidenceReference] = Field(default_factory=list)


class RerunAttemptSummary(BaseModel):
    """One attempt's identity, confidence, uncertainty and provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: uuid.UUID
    proposal_id: uuid.UUID
    status: Literal["completed", "completed_with_gaps"]
    confidence: ProposalConfidence
    uncertainty: list[str] = Field(default_factory=list)
    provenance: list[EvidenceReference] = Field(default_factory=list)
    unavailable_evidence: list[UnavailableEvidence] = Field(default_factory=list)
    source_inventory: list[SourceInventoryItem] = Field(default_factory=list)


class RerunResult(BaseModel):
    """The read-only outcome of a rerun and its comparison with the prior attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: RerunContractVersion = RERUN_CONTRACT_VERSION
    proposal: ProposalContract
    prior_attempt: RerunAttemptSummary
    current_attempt: RerunAttemptSummary
    corrections: CarriedForwardCorrections
    source_changes: SourceInventoryChanges
    input_set_unchanged: bool
    element_changes: list[ElementChange] = Field(default_factory=list)
    incomplete: bool
    incomplete_reasons: list[str] = Field(default_factory=list)


def rerun_proposal(
    prior_proposal: ProposalContract,
    fresh_proposal: ProposalContract,
    *,
    corrected_derived_elements: list[DerivedElementEdit] | None = None,
    removed_derived_element_ids: list[uuid.UUID] | None = None,
) -> RerunResult:
    """Carry *prior_proposal*'s corrections onto *fresh_proposal* and compare them.

    The prior contract is never mutated: corrections are applied to copies of
    both sides. A correction targets a prior derived element by id and is
    re-applied to the fresh element carrying the same statement; a removal is
    re-applied the same way. Corrections that the fresh attempt does not
    reproduce are recorded as unmatched rather than silently dropped.

    Raises :class:`RerunError` when a correction names an id the prior proposal
    does not carry, duplicates a correction, or would collapse two distinct
    derived elements onto one statement.
    """
    edits = list(corrected_derived_elements or [])
    removed_ids = list(removed_derived_element_ids or [])

    prior_by_id = _elements_by_id(prior_proposal.derived_elements)
    try:
        effective_prior = _apply_corrections(prior_proposal, edits, removed_ids)
    except ProposalReviewError as exc:
        raise RerunError(str(exc)) from exc

    fresh_derived, corrections = _carry_corrections_forward(
        fresh_proposal.derived_elements,
        prior_by_id=prior_by_id,
        edits=edits,
        removed_ids=removed_ids,
    )
    fresh_corrected = _with_derived_elements(fresh_proposal, fresh_derived)

    source_changes = _source_inventory_changes(
        prior_proposal.attempt.source_inventory,
        fresh_proposal.attempt.source_inventory,
    )
    input_set_unchanged = _changes_are_empty(source_changes)

    element_changes = _classify_elements(
        effective_prior.derived_elements,
        fresh_corrected.derived_elements,
        inputs_unchanged=input_set_unchanged,
    )
    incomplete_reasons = _incomplete_reasons(fresh_corrected, source_changes)

    return RerunResult(
        proposal=fresh_corrected,
        prior_attempt=_attempt_summary(prior_proposal),
        current_attempt=_attempt_summary(fresh_corrected),
        corrections=corrections,
        source_changes=source_changes,
        input_set_unchanged=input_set_unchanged,
        element_changes=element_changes,
        incomplete=bool(incomplete_reasons),
        incomplete_reasons=incomplete_reasons,
    )


def _attempt_summary(proposal: ProposalContract) -> RerunAttemptSummary:
    """Copy one attempt's identity, confidence and provenance."""
    return RerunAttemptSummary(
        attempt_id=proposal.attempt.id,
        proposal_id=proposal.proposal_id,
        status=proposal.attempt.status,
        confidence=proposal.confidence,
        uncertainty=list(proposal.uncertainty),
        provenance=list(proposal.provenance),
        unavailable_evidence=list(proposal.unavailable_evidence),
        source_inventory=list(proposal.attempt.source_inventory),
    )


def _elements_by_id(elements: list[ProposalFact]) -> dict[uuid.UUID, ProposalFact]:
    """Index derived elements by id, refusing a prior attempt with duplicate ids."""
    by_id: dict[uuid.UUID, ProposalFact] = {}
    for fact in elements:
        if fact.id in by_id:
            raise RerunError("the prior proposal carries duplicate derived element ids")
        by_id[fact.id] = fact
    return by_id


def _carry_corrections_forward(
    fresh_elements: list[ProposalFact],
    *,
    prior_by_id: dict[uuid.UUID, ProposalFact],
    edits: list[DerivedElementEdit],
    removed_ids: list[uuid.UUID],
) -> tuple[list[ProposalFact], CarriedForwardCorrections]:
    """Re-apply the prior attempt's corrections to the fresh derived elements."""
    fresh_by_statement = _elements_by_statement(fresh_elements, source="fresh")
    updated = dict(fresh_by_statement)

    corrected_ids: list[uuid.UUID] = []
    unmatched_corrected: list[uuid.UUID] = []
    for edit in edits:
        prior_statement = prior_by_id[edit.id].statement
        target = fresh_by_statement.get(prior_statement)
        if target is None:
            unmatched_corrected.append(edit.id)
            continue
        corrected = _corrected_fact(target, edit)
        if corrected.statement != target.statement and corrected.statement in updated:
            raise RerunError("a correction would collapse two derived elements onto one statement")
        del updated[prior_statement]
        updated[corrected.statement] = corrected
        corrected_ids.append(edit.id)

    removed_applied: list[uuid.UUID] = []
    unmatched_removed: list[uuid.UUID] = []
    for element_id in removed_ids:
        prior_statement = prior_by_id[element_id].statement
        if prior_statement in updated:
            del updated[prior_statement]
            removed_applied.append(element_id)
        else:
            unmatched_removed.append(element_id)

    corrections = CarriedForwardCorrections(
        corrected_derived_element_ids=corrected_ids,
        removed_derived_element_ids=removed_applied,
        unmatched_corrected_derived_element_ids=unmatched_corrected,
        unmatched_removed_derived_element_ids=unmatched_removed,
    )
    return list(updated.values()), corrections


def _with_derived_elements(proposal: ProposalContract, derived: list[ProposalFact]) -> ProposalContract:
    """Return a copy of *proposal* carrying *derived*, revalidated as a contract."""
    updated = proposal.model_copy(update={"derived_elements": derived})
    return ProposalContract.model_validate(updated.model_dump())


def _elements_by_statement(elements: list[ProposalFact], *, source: str) -> dict[str, ProposalFact]:
    """Index elements by statement, refusing an ambiguous (duplicate) statement."""
    by_statement: dict[str, ProposalFact] = {}
    for fact in elements:
        if fact.statement in by_statement:
            raise RerunError(f"the {source} proposal carries duplicate derived element statements")
        by_statement[fact.statement] = fact
    return by_statement


def _source_inventory_changes(
    prior_items: list[SourceInventoryItem],
    current_items: list[SourceInventoryItem],
) -> SourceInventoryChanges:
    """Diff the prior and current source inventories by ``source_id``."""
    prior_by_id = _inventory_by_id(prior_items, side="prior")
    current_by_id = _inventory_by_id(current_items, side="current")
    prior_ids = set(prior_by_id)
    current_ids = set(current_by_id)

    newly_available: list[str] = []
    newly_unavailable: list[str] = []
    sample_count_changes: list[SourceSampleCountChange] = []
    for source_id in sorted(prior_ids & current_ids):
        prior = prior_by_id[source_id]
        current = current_by_id[source_id]
        if prior.status == "unavailable" and current.status == "available":
            newly_available.append(source_id)
        elif prior.status == "available" and current.status == "unavailable":
            newly_unavailable.append(source_id)
        if prior.sample_count != current.sample_count:
            sample_count_changes.append(
                SourceSampleCountChange(
                    source_id=source_id,
                    prior_sample_count=prior.sample_count,
                    current_sample_count=current.sample_count,
                )
            )

    return SourceInventoryChanges(
        new_sources=[current_by_id[source_id] for source_id in sorted(current_ids - prior_ids)],
        missing_sources=[prior_by_id[source_id] for source_id in sorted(prior_ids - current_ids)],
        newly_available=newly_available,
        newly_unavailable=newly_unavailable,
        sample_count_changes=sample_count_changes,
    )


def _inventory_by_id(items: list[SourceInventoryItem], *, side: str) -> dict[str, SourceInventoryItem]:
    """Index inventory items by ``source_id``, refusing a duplicate."""
    by_id: dict[str, SourceInventoryItem] = {}
    for item in items:
        if item.source_id in by_id:
            raise RerunError(f"the {side} source inventory carries duplicate source ids")
        by_id[item.source_id] = item
    return by_id


def _changes_are_empty(changes: SourceInventoryChanges) -> bool:
    """True when no sampled-input difference was observed."""
    return not (changes.new_sources or changes.missing_sources or changes.newly_available or changes.newly_unavailable or changes.sample_count_changes)


def _classify_elements(
    prior_elements: list[ProposalFact],
    current_elements: list[ProposalFact],
    *,
    inputs_unchanged: bool,
) -> list[ElementChange]:
    """Classify every prior/current inference, deterministically ordered by statement."""
    prior_by_statement = _elements_by_statement(prior_elements, source="prior")
    current_by_statement = _elements_by_statement(current_elements, source="current")

    changes: list[ElementChange] = []
    for statement in sorted(set(prior_by_statement) | set(current_by_statement)):
        prior = prior_by_statement.get(statement)
        current = current_by_statement.get(statement)
        if prior is not None and current is not None:
            changes.append(_paired_change(prior, current))
        elif current is not None:
            changes.append(_new_change(current))
        else:
            # The statement came from the union of both indexes, so a statement
            # that is neither paired nor current-only must be prior-only.
            prior_only = prior_by_statement[statement]
            absent_status: ElementChangeStatus = "not_reproduced" if inputs_unchanged else "removed"
            changes.append(_absent_change(absent_status, prior_only))
    return changes


def _paired_change(prior: ProposalFact, current: ProposalFact) -> ElementChange:
    """Classify an inference present on both sides."""
    status: ElementChangeStatus = _paired_status(prior, current)
    return ElementChange(
        status=status,
        statement=current.statement,
        prior_element_id=prior.id,
        current_element_id=current.id,
        prior_confidence=prior.confidence,
        current_confidence=current.confidence,
        prior_evidence_refs=list(prior.evidence_refs),
        current_evidence_refs=list(current.evidence_refs),
    )


def _paired_status(prior: ProposalFact, current: ProposalFact) -> ElementChangeStatus:
    """Confidence change first, then evidence change, else retained."""
    if prior.confidence != current.confidence:
        return "confidence_changed"
    if _evidence_key(prior.evidence_refs) != _evidence_key(current.evidence_refs):
        return "evidence_changed"
    return "retained"


def _new_change(element: ProposalFact) -> ElementChange:
    """An inference the fresh attempt produced that the prior attempt did not carry."""
    return ElementChange(
        status="new",
        statement=element.statement,
        current_element_id=element.id,
        current_confidence=element.confidence,
        current_evidence_refs=list(element.evidence_refs),
    )


def _absent_change(status: ElementChangeStatus, prior: ProposalFact) -> ElementChange:
    """A prior inference the fresh attempt did not reproduce (``not_reproduced`` / ``removed``)."""
    return ElementChange(
        status=status,
        statement=prior.statement,
        prior_element_id=prior.id,
        prior_confidence=prior.confidence,
        prior_evidence_refs=list(prior.evidence_refs),
    )


def _evidence_key(refs: list[EvidenceReference]) -> list[tuple[str, str, str, str]]:
    """Order-independent identity of an element's evidence references."""
    return sorted((ref.reference_id, ref.source_id, ref.locator, ref.provenance) for ref in refs)


def _incomplete_reasons(
    fresh_proposal: ProposalContract,
    source_changes: SourceInventoryChanges,
) -> list[str]:
    """Explicit reasons the rerun is incomplete; empty when it is a full result."""
    reasons: list[str] = []
    if fresh_proposal.attempt.status == "completed_with_gaps":
        reasons.append("the fresh attempt completed with gaps")
    reasons.extend(f"source no longer sampled: {item.source_id}" for item in source_changes.missing_sources)
    return reasons
