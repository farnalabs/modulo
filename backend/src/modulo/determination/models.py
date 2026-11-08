"""Versioned contracts for editable, evidence-backed determination proposals."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProposalConfidence = Literal["high", "medium", "low", "unknown"]
ProvenanceKind = Literal["observed", "derived", "inferred", "unavailable"]
ReviewState = Literal["draft", "needs_review", "approved", "rejected"]


class EvidenceReference(BaseModel):
    """A reference to evidence without copying or upgrading the source claim."""

    model_config = ConfigDict(extra="forbid")

    reference_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    provenance: ProvenanceKind


class ProposalFact(BaseModel):
    """An observed or derived statement used by a proposal."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    statement: str = Field(min_length=1)
    kind: Literal["observed", "derived"]
    confidence: ProposalConfidence
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)


class UnresolvedAssumption(BaseModel):
    """A condition that must be reviewed before a draft can be activated."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    statement: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    confidence: ProposalConfidence = "low"
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)


class UnavailableEvidence(BaseModel):
    """A missing evidence source that limits the determination."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    requested_resource: str = Field(min_length=1)


class SourceInventoryItem(BaseModel):
    """Inventory of every source sampled for this proposal."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    connector_type: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    status: Literal["available", "unavailable"]
    error: str | None = None

    @field_validator("error")
    @classmethod
    def error_requires_unavailable(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("error must be omitted or non-empty")
        return value


class DeterminationAttempt(BaseModel):
    """One read-only determination attempt and its bounded source inventory."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    status: Literal["completed", "completed_with_gaps"]
    source_inventory: list[SourceInventoryItem] = Field(min_length=1)


class ProposalContract(BaseModel):
    """The Alpha contract for an editable draft; it never activates a pipeline."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["alpha.determination-proposal.v1"] = "alpha.determination-proposal.v1"
    proposal_id: uuid.UUID
    attempt: DeterminationAttempt
    review_state: ReviewState = "needs_review"
    activation_status: Literal["not_activated"] = "not_activated"
    observed_facts: list[ProposalFact] = Field(default_factory=list)
    derived_elements: list[ProposalFact] = Field(default_factory=list)
    unresolved_assumptions: list[UnresolvedAssumption] = Field(default_factory=list)
    unavailable_evidence: list[UnavailableEvidence] = Field(default_factory=list)
    confidence: ProposalConfidence
    uncertainty: list[str] = Field(default_factory=list)
    provenance: list[EvidenceReference] = Field(default_factory=list)

    @field_validator("derived_elements")
    @classmethod
    def derived_elements_are_derived(cls, value: list[ProposalFact]) -> list[ProposalFact]:
        if any(item.kind != "derived" for item in value):
            raise ValueError("derived_elements must contain only derived facts")
        return value

    @field_validator("observed_facts")
    @classmethod
    def observed_facts_are_observed(cls, value: list[ProposalFact]) -> list[ProposalFact]:
        if any(item.kind != "observed" for item in value):
            raise ValueError("observed_facts must contain only observed facts")
        return value
