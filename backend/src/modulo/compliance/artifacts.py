"""Artefact-type registry for the compliance control-mapping pack (FAR-2036).

Every mapping row names the artefact type that evidences it. This module is the
single vocabulary for those types, shared by the mapping-pack loader
(:mod:`modulo.compliance.pack`) and the per-instance evidence index
(:mod:`modulo.compliance.evidence_index`).

Two classes of artefact:

* **Indexed artefacts** (``indexed=True``) - the per-instance evidence index
  must account for every one of them for every organisation. A type the
  instance can observe (``instance_observable=True``) is reported ``present``
  when it has records and ``absent`` when it does not - never omitted. The
  release SBOM is indexed but not instance-observable, so it is always reported
  ``customer_supplied``.
* **Customer artefacts** (``indexed=False``) - evidence only the customer can
  produce (their own policies, risk assessments, internal-audit records,
  incident and restore-test records). Modulo never claims to hold these.

Provenance vocabulary (a mapping row's evidence provenance):

* ``observed`` - the artefact records the fact directly as Modulo saw it.
* ``derived`` - the artefact is computed from Modulo-observed records.
* ``customer-supplied`` - the evidence must come from the customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PROVENANCE_OBSERVED: Final[str] = "observed"
PROVENANCE_DERIVED: Final[str] = "derived"
PROVENANCE_CUSTOMER_SUPPLIED: Final[str] = "customer-supplied"

PROVENANCE_VALUES: Final[tuple[str, ...]] = (
    PROVENANCE_OBSERVED,
    PROVENANCE_DERIVED,
    PROVENANCE_CUSTOMER_SUPPLIED,
)

#: Coverage attribution vocabulary. Coverage is per ROW, per framework - the
#: pack deliberately has no single "compliant" verdict.
COVERAGE_SATISFIED: Final[str] = "satisfied"
COVERAGE_SUPPORTED: Final[str] = "supported"
COVERAGE_CUSTOMER_RESPONSIBILITY: Final[str] = "customer-responsibility"
COVERAGE_GAP: Final[str] = "gap"

COVERAGE_VALUES: Final[tuple[str, ...]] = (
    COVERAGE_SATISFIED,
    COVERAGE_SUPPORTED,
    COVERAGE_CUSTOMER_RESPONSIBILITY,
    COVERAGE_GAP,
)

#: Coverage values that name something Modulo does not itself satisfy. Every
#: framework must contain at least one such row - a pack that claims everything
#: is worse than no pack (enforced at load time).
NON_SELF_COVERAGE_VALUES: Final[frozenset[str]] = frozenset({COVERAGE_CUSTOMER_RESPONSIBILITY, COVERAGE_GAP})


@dataclass(frozen=True)
class ArtifactTypeSpec:
    """One artefact type in the pack's evidence vocabulary.

    ``indexed`` means the per-instance evidence index must account for the type
    (a mapped type with no records is reported as ``absent``, never omitted).
    ``instance_observable`` means the running instance itself records the
    artefact; when it is False the index reports the entry as
    ``customer_supplied`` - the operator retains it, Modulo cannot observe it.
    """

    artifact_type: str
    description: str
    indexed: bool
    instance_observable: bool
    provenance: str


ARTIFACT_AUDIT_CHAIN: Final[str] = "audit_chain"
ARTIFACT_RUN_SNAPSHOT: Final[str] = "run_snapshot"
ARTIFACT_HITL_DECISION: Final[str] = "hitl_decision"
ARTIFACT_EVAL_RESULT: Final[str] = "eval_result"
ARTIFACT_AUTONOMY_CHANGE: Final[str] = "autonomy_change"
ARTIFACT_RELEASE_SBOM: Final[str] = "release_sbom"
ARTIFACT_BUILD_IDENTITY: Final[str] = "build_identity"
ARTIFACT_CUSTOMER_RECORDS: Final[str] = "customer_records"


ARTIFACT_TYPES: Final[dict[str, ArtifactTypeSpec]] = {
    ARTIFACT_AUDIT_CHAIN: ArtifactTypeSpec(
        artifact_type=ARTIFACT_AUDIT_CHAIN,
        description=(
            "The organisation's hash-chained, append-only audit trail. A ``present`` "
            "status means the trail exists in this instance; it does NOT assert that "
            "the offline-verifiable evidence pack export is available here, which "
            "additionally needs the Team-tier audit_viewer feature and a configured "
            "MODULO_EVIDENCE_SIGNING_KEY."
        ),
        indexed=True,
        instance_observable=True,
        provenance=PROVENANCE_OBSERVED,
    ),
    ARTIFACT_RUN_SNAPSHOT: ArtifactTypeSpec(
        artifact_type=ARTIFACT_RUN_SNAPSHOT,
        description=(
            "The immutable pipeline snapshot a run executed against - the frozen graph, "
            "model, schema, guardrail and policy-gate pins."
        ),
        indexed=True,
        instance_observable=True,
        provenance=PROVENANCE_OBSERVED,
    ),
    ARTIFACT_HITL_DECISION: ArtifactTypeSpec(
        artifact_type=ARTIFACT_HITL_DECISION,
        description=(
            "Recorded human-in-the-loop decisions. Gates marked human_only (the "
            "fail-safe default) reject API keys and programmatic tokens over REST and "
            "MCP; a browser session may decide them, and so may a consent-derived, "
            "human-linked OAuth credential over MCP while the default-on "
            "mcp_human_linked_hitl_authority flag is enabled. deliver_manual stays "
            "browser-only. A gate is only reached at all when the pipeline's autonomy "
            "level is manual_approval - notify_on_complete auto-approves it and "
            "fully_autonomous skips it."
        ),
        indexed=True,
        instance_observable=True,
        provenance=PROVENANCE_OBSERVED,
    ),
    ARTIFACT_EVAL_RESULT: ArtifactTypeSpec(
        artifact_type=ARTIFACT_EVAL_RESULT,
        description=("Persisted evaluation results, each pinned to the eval-definition version that scored it."),
        indexed=True,
        instance_observable=True,
        provenance=PROVENANCE_OBSERVED,
    ),
    ARTIFACT_AUTONOMY_CHANGE: ArtifactTypeSpec(
        artifact_type=ARTIFACT_AUTONOMY_CHANGE,
        description=(
            "Audited pipeline autonomy promotions and demotions, with the previous and new "
            "level and the recorded reason."
        ),
        indexed=True,
        instance_observable=True,
        provenance=PROVENANCE_OBSERVED,
    ),
    ARTIFACT_RELEASE_SBOM: ArtifactTypeSpec(
        artifact_type=ARTIFACT_RELEASE_SBOM,
        description=(
            "The CycloneDX software bill of materials for the deployed release. Produced by "
            "the release pipeline or by the shipped generator; retained by the operator."
        ),
        indexed=True,
        instance_observable=False,
        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
    ),
    ARTIFACT_BUILD_IDENTITY: ArtifactTypeSpec(
        artifact_type=ARTIFACT_BUILD_IDENTITY,
        description=(
            "The identity of the running build: product version, source revision and build "
            "timestamp, as reported by the deployment endpoint."
        ),
        indexed=True,
        instance_observable=True,
        provenance=PROVENANCE_OBSERVED,
    ),
    ARTIFACT_CUSTOMER_RECORDS: ArtifactTypeSpec(
        artifact_type=ARTIFACT_CUSTOMER_RECORDS,
        description=(
            "Records the customer produces and retains outside Modulo: policies, risk "
            "assessments, internal-audit reports, vulnerability-management records, "
            "incident and restore-test records."
        ),
        indexed=False,
        instance_observable=False,
        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
    ),
}

#: The artefact types the per-instance evidence index MUST account for, in
#: report order. A mapped type with no records is surfaced as ``absent``.
INDEXED_ARTIFACT_TYPES: Final[tuple[str, ...]] = tuple(
    artifact_type for artifact_type, spec in ARTIFACT_TYPES.items() if spec.indexed
)
