"""Per-instance compliance evidence index (FAR-2036).

Given an organisation, :func:`build_evidence_index` lists the evidence
artefacts that exist in *that instance*, each keyed to the mapping rows it
satisfies. This is the practical artefact an auditor can be handed: it names
what Modulo observed, where to find it, and what is missing.

Design rules (each is a requirement, not a preference):

* **Read existing data.** No new tables. Every artefact is read from data the
  product already records.
* **Never silently omit.** Every artefact type in
  :data:`~modulo.compliance.artifacts.INDEXED_ARTIFACT_TYPES` appears in the
  index. A type with no records is reported ``absent`` with a reason;
  ``strict=True`` turns the same condition into a raised
  :class:`EvidenceIndexIncompleteError`.
* **Provenance is explicit.** Each entry says whether Modulo observed it,
  derived it, or whether the customer must supply it.
* **The pack version is recorded.** ``pack_version`` and
  ``pack_content_hash`` travel with the index, so a later release cannot
  silently change what was asserted for a past audit period.
* **No cross-tenant reads.** Every query filters on ``organisation_id``; the
  organisation is always the caller's principal's organisation.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.compliance.artifacts import (
    ARTIFACT_AUDIT_CHAIN,
    ARTIFACT_AUTONOMY_CHANGE,
    ARTIFACT_BUILD_IDENTITY,
    ARTIFACT_EVAL_RESULT,
    ARTIFACT_HITL_DECISION,
    ARTIFACT_RELEASE_SBOM,
    ARTIFACT_RUN_SNAPSHOT,
    ARTIFACT_TYPES,
    INDEXED_ARTIFACT_TYPES,
    PROVENANCE_CUSTOMER_SUPPLIED,
    PROVENANCE_OBSERVED,
)
from modulo.compliance.pack import MappingPack, load_mapping_pack
from modulo.core.audit_logger import get_chain_head
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.version import get_version

_log = logging.getLogger(__name__)

STATUS_PRESENT: Final[str] = "present"
STATUS_ABSENT: Final[str] = "absent"
STATUS_CUSTOMER_SUPPLIED: Final[str] = "customer_supplied"

STATUS_VALUES: Final[tuple[str, ...]] = (
    STATUS_PRESENT,
    STATUS_ABSENT,
    STATUS_CUSTOMER_SUPPLIED,
)

#: The audit event types that record an autonomy promotion or demotion. They
#: are written by the pipeline autonomy routes; a test pins these literals
#: against that module so a rename cannot silently empty this artefact.
AUTONOMY_PROMOTION_EVENT: Final[str] = "pipeline.autonomy_promotion_decided"
AUTONOMY_DEMOTION_EVENT: Final[str] = "pipeline.autonomy_demoted"
AUTONOMY_EVENT_TYPES: Final[tuple[str, ...]] = (
    AUTONOMY_PROMOTION_EVENT,
    AUTONOMY_DEMOTION_EVENT,
)

#: Bounded sample of identifiers returned per artefact entry. The index is an
#: auditable inventory, not a bulk export - counts are always exact, the sample
#: is capped so one very large organisation cannot force an unbounded response.
_SAMPLE_LIMIT: Final[int] = 20

#: Bounded scan of decided HITL rows used to count human-only approvals. The
#: gate config is JSON, and the count is computed in Python so the query works
#: identically on Postgres and SQLite. If an organisation has more decided gates
#: than this, the entry says so in its detail rather than reporting a silent
#: undercount.
_HITL_SCAN_LIMIT: Final[int] = 10000


class EvidenceIndexIncompleteError(RuntimeError):
    """A mapped artefact type has no records for the organisation.

    Raised only by ``build_evidence_index(strict=True)``. The non-strict path
    reports the same condition as an ``absent`` entry, so callers that want to
    show an honest partial index still can.
    """

    def __init__(self, missing_artifact_types: tuple[str, ...]) -> None:
        self.missing_artifact_types = missing_artifact_types
        super().__init__(
            "evidence index is incomplete; no records for mapped artefact type(s): " + ", ".join(missing_artifact_types)
        )


class ArtifactEntry(BaseModel):
    """One evidence artefact in the index, keyed to the clauses it evidences."""

    model_config = ConfigDict(frozen=True)

    artifact_type: str
    status: str
    provenance: str
    description: str
    location: str
    identifiers: tuple[str, ...]
    record_count: int | None
    detail: dict[str, Any]
    mapping_row_ids: tuple[str, ...]
    absence_reason: str | None = None

    @model_validator(mode="after")
    def _check_status_shape(self) -> ArtifactEntry:
        if self.status not in STATUS_VALUES:
            raise ValueError(f"invalid artefact status {self.status!r}")
        if self.provenance not in {
            PROVENANCE_OBSERVED,
            PROVENANCE_CUSTOMER_SUPPLIED,
            "derived",
        }:
            raise ValueError(f"invalid artefact provenance {self.provenance!r}")
        if not self.mapping_row_ids:
            raise ValueError(f"artefact {self.artifact_type!r} is not keyed to any mapping row")
        if self.status == STATUS_PRESENT and not self.identifiers:
            raise ValueError(f"artefact {self.artifact_type!r} is present but names no identifier")
        if self.status == STATUS_ABSENT and not (self.absence_reason or "").strip():
            raise ValueError(f"artefact {self.artifact_type!r} is absent but states no reason")
        return self


class EvidenceIndex(BaseModel):
    """The generated per-instance evidence index."""

    model_config = ConfigDict(frozen=True)

    pack_version: str
    pack_content_hash: str
    pack_published_at: str
    generated_at: datetime
    product_version: str
    organisation_id: str
    framework_ids: tuple[str, ...]
    coverage: dict[str, dict[str, int]]
    complete: bool
    missing_artifact_types: tuple[str, ...]
    artifacts: tuple[ArtifactEntry, ...]


def _entries_for_rows(pack: MappingPack, artifact_type: str) -> tuple[str, ...]:
    return pack.mapping_row_ids_for_artifact(artifact_type)


def _present(
    *,
    pack: MappingPack,
    artifact_type: str,
    location: str,
    identifiers: list[str],
    record_count: int,
    detail: dict[str, Any],
) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_type=artifact_type,
        status=STATUS_PRESENT,
        provenance=ARTIFACT_TYPES[artifact_type].provenance,
        description=ARTIFACT_TYPES[artifact_type].description,
        location=location,
        identifiers=tuple(identifiers[:_SAMPLE_LIMIT]),
        record_count=record_count,
        detail=detail,
        mapping_row_ids=_entries_for_rows(pack, artifact_type),
    )


def _absent(
    *,
    pack: MappingPack,
    artifact_type: str,
    location: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_type=artifact_type,
        status=STATUS_ABSENT,
        provenance=ARTIFACT_TYPES[artifact_type].provenance,
        description=ARTIFACT_TYPES[artifact_type].description,
        location=location,
        identifiers=(),
        record_count=0,
        detail=detail or {},
        mapping_row_ids=_entries_for_rows(pack, artifact_type),
        absence_reason=reason,
    )


def _iso(value: datetime | None) -> str | None:
    """Render a timestamp deterministically, normalising SQLite's naive values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.isoformat()


# ---------------------------------------------------------------------------
# Collectors - one per indexed artefact type
# ---------------------------------------------------------------------------


async def _collect_audit_chain(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
) -> ArtifactEntry:
    location = (
        "GET /api/v1/admin/audit (browse), GET /api/v1/admin/audit/verify (integrity), "
        "GET /api/v1/admin/audit/evidence-pack (signed offline-verifiable export)"
    )
    head = await get_chain_head(session, organisation_id)
    stats = (
        await session.execute(
            select(
                func.count(AuditEvent.id),
                func.min(AuditEvent.created_at),
                func.max(AuditEvent.created_at),
            ).where(AuditEvent.organisation_id == organisation_id)
        )
    ).one()
    event_count = int(stats[0] or 0)
    if head is None or event_count == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_AUDIT_CHAIN,
            location=location,
            reason="this instance holds no audit events for the organisation yet",
            detail={"event_count": event_count},
        )

    identifiers = [f"audit-chain-head:{head.last_event_hash}"]
    if head.last_event_id is not None:
        identifiers.append(f"audit-chain-last-event:{head.last_event_id}")
    return _present(
        pack=pack,
        artifact_type=ARTIFACT_AUDIT_CHAIN,
        location=location,
        identifiers=identifiers,
        record_count=event_count,
        detail={
            "chain_head_hash": head.last_event_hash,
            "chain_head_event_count": int(head.event_count),
            "event_count": event_count,
            "first_event_at": _iso(stats[1]),
            "last_event_at": _iso(stats[2]),
        },
    )


async def _collect_run_snapshots(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
) -> ArtifactEntry:
    location = "GET /api/v1/pipelines/{pipeline_id}/snapshots and the snapshot_id on GET /api/v1/runs/{run_id}"
    snapshot_count = int(
        (
            await session.execute(
                select(func.count(func.distinct(Run.snapshot_id))).where(Run.organisation_id == organisation_id)
            )
        ).scalar_one()
        or 0
    )
    if snapshot_count == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_RUN_SNAPSHOT,
            location=location,
            reason="no run in this instance has executed against a frozen pipeline snapshot",
        )

    rows = (
        await session.execute(
            select(
                PipelineSnapshot.id,
                PipelineSnapshot.snapshot_version,
                PipelineSnapshot.pipeline_id,
                PipelineSnapshot.created_at,
            )
            .join(Run, Run.snapshot_id == PipelineSnapshot.id)
            .where(Run.organisation_id == organisation_id)
            .distinct()
            .order_by(PipelineSnapshot.created_at.desc())
            .limit(_SAMPLE_LIMIT)
        )
    ).all()
    identifiers = [f"pipeline-snapshot:{row[0]}@v{row[1]}" for row in rows]
    return _present(
        pack=pack,
        artifact_type=ARTIFACT_RUN_SNAPSHOT,
        location=location,
        identifiers=identifiers,
        record_count=snapshot_count,
        detail={
            "distinct_snapshots": snapshot_count,
            "sample_pipeline_ids": [str(row[2]) for row in rows],
        },
    )


async def _collect_hitl_decisions(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
) -> ArtifactEntry:
    location = (
        "GET /api/v1/hitl/reviews and GET /api/v1/runs/{run_id}/hitl/pending; the "
        "decision is stored on the HITL claim row"
    )
    by_decision = (
        await session.execute(
            select(HitlClaim.decision, func.count(HitlClaim.id))
            .where(
                HitlClaim.organisation_id == organisation_id,
                HitlClaim.decision.is_not(None),
            )
            .group_by(HitlClaim.decision)
        )
    ).all()
    counts = {str(row[0]): int(row[1]) for row in by_decision}
    total = sum(counts.values())
    if total == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_HITL_DECISION,
            location=location,
            reason="no human-in-the-loop gate has been decided in this instance",
        )

    decided_rows = (
        await session.execute(
            select(
                HitlClaim.id,
                HitlClaim.decision,
                HitlClaim.decision_at,
                HitlClaim.decided_by,
                HitlClaim.gate_config_json,
            )
            .where(
                HitlClaim.organisation_id == organisation_id,
                HitlClaim.decision.is_not(None),
            )
            .order_by(HitlClaim.decision_at.desc())
            .limit(_HITL_SCAN_LIMIT)
        )
    ).all()
    human_only_approved = 0
    human_only_total = 0
    for row in decided_rows:
        gate_config = row[4] if isinstance(row[4], dict) else {}
        if gate_config.get("human_only") is not True:
            continue
        human_only_total += 1
        if row[1] == "approved":
            human_only_approved += 1

    latest = decided_rows[0][2] if decided_rows else None
    identifiers = [f"hitl-decision:{row[0]}" for row in decided_rows[:_SAMPLE_LIMIT]]
    return _present(
        pack=pack,
        artifact_type=ARTIFACT_HITL_DECISION,
        location=location,
        identifiers=identifiers,
        record_count=total,
        detail={
            "decisions_by_outcome": counts,
            "human_only_decisions": human_only_total,
            "human_only_approvals": human_only_approved,
            "human_only_scan_truncated": total > _HITL_SCAN_LIMIT,
            "latest_decision_at": _iso(latest),
        },
    )


async def _collect_eval_results(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
) -> ArtifactEntry:
    location = (
        "GET /api/v1/runs/{run_id}/evals and GET /api/v1/eval-suites; each result "
        "records the eval-definition version that scored it"
    )
    stats = (
        await session.execute(
            select(
                func.count(EvalResult.id),
                func.sum(case((EvalResult.passed.is_(True), 1), else_=0)),
                func.max(EvalResult.evaluated_at),
                func.count(func.distinct(EvalResult.eval_id)),
            ).where(EvalResult.organisation_id == organisation_id)
        )
    ).one()
    total = int(stats[0] or 0)
    if total == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_EVAL_RESULT,
            location=location,
            reason="no evaluation has been recorded in this instance",
        )

    passed = int(stats[1] or 0)
    sample_ids = (
        (
            await session.execute(
                select(EvalResult.id)
                .where(EvalResult.organisation_id == organisation_id)
                .order_by(EvalResult.evaluated_at.desc())
                .limit(_SAMPLE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return _present(
        pack=pack,
        artifact_type=ARTIFACT_EVAL_RESULT,
        location=location,
        identifiers=[f"eval-result:{result_id}" for result_id in sample_ids],
        record_count=total,
        detail={
            "passed": passed,
            "failed": total - passed,
            "distinct_eval_definitions": int(stats[3] or 0),
            "latest_evaluated_at": _iso(stats[2]),
        },
    )


async def _collect_autonomy_changes(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
) -> ArtifactEntry:
    location = (
        "Audit events pipeline.autonomy_promotion_decided and pipeline.autonomy_demoted at GET /api/v1/admin/audit"
    )
    by_type = (
        await session.execute(
            select(AuditEvent.event_type, func.count(AuditEvent.id))
            .where(
                AuditEvent.organisation_id == organisation_id,
                AuditEvent.event_type.in_(AUTONOMY_EVENT_TYPES),
            )
            .group_by(AuditEvent.event_type)
        )
    ).all()
    counts = {str(row[0]): int(row[1]) for row in by_type}
    total = sum(counts.values())
    if total == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_AUTONOMY_CHANGE,
            location=location,
            reason="no pipeline autonomy level has been promoted or demoted in this instance",
        )

    sample_ids = (
        (
            await session.execute(
                select(AuditEvent.id)
                .where(
                    AuditEvent.organisation_id == organisation_id,
                    AuditEvent.event_type.in_(AUTONOMY_EVENT_TYPES),
                )
                .order_by(AuditEvent.created_at.desc())
                .limit(_SAMPLE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return _present(
        pack=pack,
        artifact_type=ARTIFACT_AUTONOMY_CHANGE,
        location=location,
        identifiers=[f"audit-event:{event_id}" for event_id in sample_ids],
        record_count=total,
        detail={"counts_by_event_type": counts},
    )


def _collect_build_identity(pack: MappingPack) -> ArtifactEntry:
    location = "GET /api/v1/deployment"
    version = get_version()
    git_sha = os.environ.get("GIT_SHA", "").strip()
    build_timestamp = os.environ.get("BUILD_TIMESTAMP", "").strip()
    if version == "0.0.0-dev":
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_BUILD_IDENTITY,
            location=location,
            reason=(
                "the running process cannot identify its release (the package reports the "
                "development version, so no version, source revision or build time is available)"
            ),
        )

    identifiers = [f"product-version:{version}"]
    if git_sha:
        identifiers.append(f"git-sha:{git_sha}")
    if build_timestamp:
        identifiers.append(f"build-timestamp:{build_timestamp}")
    missing_fields = [
        field for field, value in (("git_sha", git_sha), ("build_timestamp", build_timestamp)) if not value
    ]
    return _present(
        pack=pack,
        artifact_type=ARTIFACT_BUILD_IDENTITY,
        location=location,
        identifiers=identifiers,
        record_count=1,
        detail={
            "product_version": version,
            "git_sha": git_sha or None,
            "build_timestamp": build_timestamp or None,
            "missing_build_fields": missing_fields,
        },
    )


def _collect_release_sbom(pack: MappingPack) -> ArtifactEntry:
    """The SBOM is produced at release time and retained by the operator."""
    return ArtifactEntry(
        artifact_type=ARTIFACT_RELEASE_SBOM,
        status=STATUS_CUSTOMER_SUPPLIED,
        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
        description=ARTIFACT_TYPES[ARTIFACT_RELEASE_SBOM].description,
        location=(
            "Release pipeline artefact sbom-cyclonedx (.github/workflows/release.yml), or "
            "regenerate with python backend/scripts/generate-sbom.py"
        ),
        identifiers=(),
        record_count=None,
        detail={
            "supplied_by": "customer/operator",
            "format": "CycloneDX",
            "note": (
                "the running instance does not hold the SBOM; the operator must retain the "
                "SBOM for the release it deployed and hand it to the auditor"
            ),
        },
        mapping_row_ids=_entries_for_rows(pack, ARTIFACT_RELEASE_SBOM),
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


async def build_evidence_index(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    generated_at: datetime | None = None,
    strict: bool = False,
) -> EvidenceIndex:
    """Build the per-instance evidence index for *organisation_id*.

    The caller supplies the session and is responsible for the transaction and
    RLS context (the route does this). Every query here ALSO filters on
    ``organisation_id`` explicitly, so the index cannot widen beyond the
    caller's tenant even if a future caller forgets the RLS context.

    ``strict=True`` raises :class:`EvidenceIndexIncompleteError` when a mapped
    artefact type has no records. The default reports the same condition as an
    ``absent`` entry, which is loud in the payload without making the whole
    index unavailable.
    """
    pack = load_mapping_pack()
    artifacts = (
        await _collect_audit_chain(session, organisation_id, pack),
        await _collect_run_snapshots(session, organisation_id, pack),
        await _collect_hitl_decisions(session, organisation_id, pack),
        await _collect_eval_results(session, organisation_id, pack),
        await _collect_autonomy_changes(session, organisation_id, pack),
        _collect_release_sbom(pack),
        _collect_build_identity(pack),
    )

    covered = tuple(entry.artifact_type for entry in artifacts)
    if covered != INDEXED_ARTIFACT_TYPES:
        # A collector was added, removed or renamed without updating the pack
        # vocabulary. Fail rather than serve an index with a silent hole.
        raise RuntimeError(
            "evidence index artifact coverage does not match the indexed vocabulary: "
            f"index={covered} expected={INDEXED_ARTIFACT_TYPES}"
        )

    missing = tuple(entry.artifact_type for entry in artifacts if entry.status == STATUS_ABSENT)
    if missing:
        _log.warning(
            "compliance.evidence_index.incomplete organisation_id=%s missing=%s",
            organisation_id,
            ",".join(missing),
        )
        if strict:
            raise EvidenceIndexIncompleteError(missing)

    return EvidenceIndex(
        pack_version=pack.pack_version,
        pack_content_hash=pack.content_hash(),
        pack_published_at=pack.published_at,
        generated_at=generated_at or datetime.now(UTC),
        product_version=get_version(),
        organisation_id=str(organisation_id),
        framework_ids=pack.framework_ids(),
        coverage=pack.coverage_counts(),
        complete=not missing,
        missing_artifact_types=missing,
        artifacts=artifacts,
    )
