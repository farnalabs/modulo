"""Tests for rerunning a determination against a prior attempt.

The rerun is pure: it compares a caller-supplied prior contract with a freshly
built one, carries the prior attempt's corrections forward, and classifies each
inference. These tests pin attempt identity, prior immutability, the
``not_reproduced`` / ``removed`` distinction, every element status, correction
carry-forward, source-inventory differences and explicit incompleteness.
"""

from __future__ import annotations

import uuid
from typing import Literal

import pytest

from modulo.determination.inference import build_proposal_contract
from modulo.determination.models import (
    DeterminationAttempt,
    EvidenceReference,
    ProposalContract,
    ProposalFact,
    SourceInventoryItem,
)
from modulo.determination.rerun import ElementChange, RerunError, rerun_proposal
from modulo.determination.review import DerivedElementEdit
from modulo.determination.scanner import ScanSample

from .helpers import make_sample

_STATUS = Literal["completed", "completed_with_gaps"]


def _element(
    statement: str,
    *,
    confidence: str = "high",
    evidence_refs: list[EvidenceReference] | None = None,
    element_id: uuid.UUID | None = None,
) -> ProposalFact:
    return ProposalFact(
        id=element_id or uuid.uuid5(uuid.NAMESPACE_URL, statement),
        statement=statement,
        kind="derived",
        confidence=confidence,  # type: ignore[arg-type]
        evidence_refs=evidence_refs or [],
    )


def _ref(reference_id: str) -> EvidenceReference:
    return EvidenceReference(
        reference_id=reference_id,
        source_id="conn:repos",
        locator="scan:repos",
        provenance="observed",
    )


def _inventory_item(
    source_id: str = "conn:repos",
    *,
    status: Literal["available", "unavailable"] = "available",
    sample_count: int = 1,
    error: str | None = None,
) -> SourceInventoryItem:
    return SourceInventoryItem(
        source_id=source_id,
        connector_type="github",
        resource="repos",
        sample_count=sample_count,
        status=status,
        error=error,
    )


def _contract(
    derived: list[ProposalFact],
    *,
    inventory: list[SourceInventoryItem] | None = None,
    status: _STATUS = "completed",
    confidence: str = "high",
    uncertainty: list[str] | None = None,
    provenance: list[EvidenceReference] | None = None,
    attempt_id: uuid.UUID | None = None,
) -> ProposalContract:
    return ProposalContract(
        proposal_id=uuid.uuid4(),
        attempt=DeterminationAttempt(
            id=attempt_id or uuid.uuid4(),
            status=status,
            source_inventory=inventory or [_inventory_item()],
        ),
        derived_elements=derived,
        confidence=confidence,  # type: ignore[arg-type]
        uncertainty=uncertainty or [],
        provenance=provenance or [],
    )


def _status_by_statement(changes: list[ElementChange]) -> dict[str, str]:
    return {change.statement: change.status for change in changes}


class TestAttemptIdentity:
    def test_rerun_returns_the_fresh_attempt_and_never_mutates_the_prior(self) -> None:
        prior = _contract([_element("Development stage detected")], uncertainty=["sampled metadata only"])
        fresh = _contract([_element("Development stage detected")], uncertainty=["sampled metadata only"])
        prior_snapshot = prior.model_dump()

        result = rerun_proposal(prior, fresh)

        assert result.proposal.attempt.id == fresh.attempt.id
        assert result.proposal.attempt.id != prior.attempt.id
        assert result.prior_attempt.attempt_id == prior.attempt.id
        assert result.current_attempt.attempt_id == fresh.attempt.id
        assert prior.model_dump() == prior_snapshot

    def test_both_attempts_carry_their_provenance_and_uncertainty(self) -> None:
        prior = _contract(
            [_element("Development stage detected")],
            uncertainty=["prior uncertainty"],
            provenance=[_ref("evidence:prior")],
        )
        fresh = _contract(
            [_element("Development stage detected")],
            uncertainty=["fresh uncertainty"],
            provenance=[_ref("evidence:fresh")],
        )

        result = rerun_proposal(prior, fresh)

        assert result.prior_attempt.provenance == prior.provenance
        assert result.prior_attempt.uncertainty == ["prior uncertainty"]
        assert result.current_attempt.provenance == fresh.provenance
        assert result.current_attempt.uncertainty == ["fresh uncertainty"]

    def test_identical_rerun_retains_every_element(self) -> None:
        sample: ScanSample = make_sample("repos", [{"name": "owner/repo"}])
        prior = build_proposal_contract([sample])
        fresh = build_proposal_contract([sample])

        result = rerun_proposal(prior, fresh)

        assert result.input_set_unchanged
        assert result.element_changes
        assert {change.status for change in result.element_changes} == {"retained"}


class TestAbsentElementClassification:
    def test_unchanged_inputs_absent_element_is_not_reproduced(self) -> None:
        prior = _contract([_element("Kept"), _element("Ghost claim")])
        fresh = _contract([_element("Kept")])

        result = rerun_proposal(prior, fresh)

        assert result.input_set_unchanged
        assert _status_by_statement(result.element_changes)["Ghost claim"] == "not_reproduced"

    def test_changed_sample_count_absent_element_is_removed(self) -> None:
        prior = _contract(
            [_element("Kept"), _element("Ghost claim")],
            inventory=[_inventory_item(sample_count=3)],
        )
        fresh = _contract([_element("Kept")], inventory=[_inventory_item(sample_count=1)])

        result = rerun_proposal(prior, fresh)

        assert not result.input_set_unchanged
        assert result.source_changes.sample_count_changes[0].prior_sample_count == 3
        assert _status_by_statement(result.element_changes)["Ghost claim"] == "removed"


class TestElementStatuses:
    def test_retained_new_confidence_and_evidence_changes_are_distinct(self) -> None:
        prior = _contract(
            [
                _element("Retained"),
                _element("Evidence moves", evidence_refs=[_ref("evidence:one")]),
                _element("Confidence drops"),
                _element("Drops out"),
            ]
        )
        fresh = _contract(
            [
                _element("Retained"),
                _element("Evidence moves", evidence_refs=[_ref("evidence:two")]),
                _element("Confidence drops", confidence="low"),
                _element("Brand new"),
            ]
        )

        result = rerun_proposal(prior, fresh)
        statuses = _status_by_statement(result.element_changes)

        assert statuses["Retained"] == "retained"
        assert statuses["Evidence moves"] == "evidence_changed"
        assert statuses["Confidence drops"] == "confidence_changed"
        assert statuses["Brand new"] == "new"
        assert statuses["Drops out"] == "not_reproduced"

    def test_confidence_change_takes_precedence_but_both_sides_are_carried(self) -> None:
        prior = _contract([_element("Changes twice", evidence_refs=[_ref("evidence:one")])])
        fresh = _contract([_element("Changes twice", confidence="low", evidence_refs=[_ref("evidence:two")])])

        result = rerun_proposal(prior, fresh)
        change = result.element_changes[0]

        assert change.status == "confidence_changed"
        assert change.prior_confidence == "high"
        assert change.current_confidence == "low"
        assert change.prior_evidence_refs == [_ref("evidence:one")]
        assert change.current_evidence_refs == [_ref("evidence:two")]


class TestCorrectionsCarriedForward:
    def test_corrected_element_is_reapplied_to_the_fresh_attempt(self) -> None:
        element_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
        prior = _contract([_element("Development observed", element_id=element_id)])
        fresh = _contract([_element("Development observed")])
        edit = DerivedElementEdit(id=element_id, statement="Development confirmed", confidence="low")

        result = rerun_proposal(prior, fresh, corrected_derived_elements=[edit])

        assert result.corrections.corrected_derived_element_ids == [element_id]
        assert not result.corrections.unmatched_corrected_derived_element_ids
        corrected = result.proposal.derived_elements[0]
        assert corrected.statement == "Development confirmed"
        assert corrected.confidence == "low"
        # The correction is applied to both sides, so the element is retained.
        assert result.element_changes[0].status == "retained"

    def test_removed_element_stays_removed_on_the_fresh_attempt(self) -> None:
        element_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
        prior = _contract([_element("Removed claim", element_id=element_id)])
        fresh = _contract([_element("Removed claim")])

        result = rerun_proposal(prior, fresh, removed_derived_element_ids=[element_id])

        assert result.corrections.removed_derived_element_ids == [element_id]
        assert not result.proposal.derived_elements

    def test_correction_without_a_fresh_counterpart_is_recorded_as_unmatched(self) -> None:
        element_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
        prior = _contract([_element("No longer inferred", element_id=element_id)])
        fresh = _contract([_element("Something else")])
        edit = DerivedElementEdit(id=element_id, confidence="low")

        result = rerun_proposal(prior, fresh, corrected_derived_elements=[edit])

        assert result.corrections.unmatched_corrected_derived_element_ids == [element_id]
        assert not result.corrections.corrected_derived_element_ids
        assert [item.statement for item in result.proposal.derived_elements] == ["Something else"]


class TestSourceInventoryChanges:
    def test_new_source_is_reported_and_counts_as_an_input_change(self) -> None:
        prior = _contract([_element("Kept")], inventory=[_inventory_item("conn:repos")])
        fresh = _contract(
            [_element("Kept")],
            inventory=[_inventory_item("conn:repos"), _inventory_item("conn:pulls")],
        )

        result = rerun_proposal(prior, fresh)

        assert [item.source_id for item in result.source_changes.new_sources] == ["conn:pulls"]
        assert not result.input_set_unchanged

    def test_newly_available_source_is_reported(self) -> None:
        prior = _contract(
            [_element("Kept")],
            inventory=[_inventory_item("conn:repos", status="unavailable", error="boom", sample_count=0)],
        )
        fresh = _contract([_element("Kept")], inventory=[_inventory_item("conn:repos")])

        result = rerun_proposal(prior, fresh)

        assert result.source_changes.newly_available == ["conn:repos"]
        assert not result.input_set_unchanged


class TestIncompleteness:
    def test_newly_unavailable_source_marks_the_rerun_incomplete(self) -> None:
        prior = _contract([_element("Kept")], inventory=[_inventory_item("conn:repos")])
        fresh = _contract(
            [_element("Kept")],
            inventory=[_inventory_item("conn:repos", status="unavailable", error="timeout", sample_count=0)],
            status="completed_with_gaps",
        )

        result = rerun_proposal(prior, fresh)

        assert result.source_changes.newly_unavailable == ["conn:repos"]
        assert result.incomplete
        assert result.incomplete_reasons

    def test_missing_source_is_surfaced_and_marks_the_rerun_incomplete(self) -> None:
        prior = _contract(
            [_element("Kept")],
            inventory=[_inventory_item("conn:repos"), _inventory_item("conn:pulls")],
        )
        fresh = _contract([_element("Kept")], inventory=[_inventory_item("conn:repos")], status="completed")

        result = rerun_proposal(prior, fresh)

        assert [item.source_id for item in result.source_changes.missing_sources] == ["conn:pulls"]
        assert result.incomplete
        assert "source no longer sampled: conn:pulls" in result.incomplete_reasons

    def test_full_result_is_not_marked_incomplete(self) -> None:
        prior = _contract([_element("Kept")])
        fresh = _contract([_element("Kept")])

        result = rerun_proposal(prior, fresh)

        assert not result.incomplete
        assert not result.incomplete_reasons


class TestInvalidInput:
    def test_unknown_correction_id_is_refused(self) -> None:
        prior = _contract([_element("Kept")])
        fresh = _contract([_element("Kept")])
        edit = DerivedElementEdit(id=uuid.uuid4(), confidence="low")

        with pytest.raises(RerunError, match="derived elements"):
            rerun_proposal(prior, fresh, corrected_derived_elements=[edit])

    def test_duplicate_correction_ids_are_refused(self) -> None:
        element_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
        prior = _contract([_element("Kept", element_id=element_id)])
        fresh = _contract([_element("Kept")])
        edits = [
            DerivedElementEdit(id=element_id, confidence="low"),
            DerivedElementEdit(id=element_id, statement="Kept again"),
        ]

        with pytest.raises(RerunError, match="duplicate corrections"):
            rerun_proposal(prior, fresh, corrected_derived_elements=edits)

    def test_correcting_and_removing_the_same_element_is_refused(self) -> None:
        element_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
        prior = _contract([_element("Kept", element_id=element_id)])
        fresh = _contract([_element("Kept")])
        edit = DerivedElementEdit(id=element_id, confidence="low")

        with pytest.raises(RerunError, match="corrected and removed"):
            rerun_proposal(
                prior,
                fresh,
                corrected_derived_elements=[edit],
                removed_derived_element_ids=[element_id],
            )

    def test_duplicate_prior_element_ids_are_refused(self) -> None:
        element_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
        prior = _contract(
            [
                _element("First", element_id=element_id),
                _element("Second", element_id=element_id),
            ]
        )
        fresh = _contract([_element("First")])

        with pytest.raises(RerunError, match="duplicate derived element ids"):
            rerun_proposal(prior, fresh)
