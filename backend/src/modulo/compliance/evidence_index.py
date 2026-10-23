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
* **The index can be scoped to an audit period.** ``period_from`` /
  ``period_to`` bound every artefact aggregate to the half-open interval
  ``[period_from, period_to)``. The period travels in the response, so a
  scoped index can never be mistaken for a full-history one. With BOTH unset
  the index covers the organisation's **full history** - the default, and
  what every caller got before FAR-2055.
* **Aggregates are index-served and time-bounded.** The aggregate queries are
  backed by composite (and, where the query filters a nullable column,
  partial) indexes declared on the models and shipped by migration 0326; the
  route additionally caps the transaction with a Postgres
  ``statement_timeout`` so a mature instance degrades to an actionable error
  rather than holding a pooled connection open.
* **The human-oversight count uses the product's own rule.** Whether a gate is
  ``human_only`` is resolved through
  :func:`modulo.db.crud.hitl_review_config.human_only_effective` - the single
  source of truth for the fail-safe default (FAR-609). Re-implementing the rule
  here would under-report human oversight, because an omitted ``human_only``
  key means TRUE, not FALSE.
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
from modulo.db.crud.hitl_review_config import human_only_effective

# The single UTC normaliser used for naive timestamps; imported by its private
# name because the value has no shared home yet and the duplicate-logic
# baseline is shrink-only (FAR-2055).
from modulo.db.crud.token_family import _as_utc_aware
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
#: auditable inventory, not a bulk export - the SAMPLE is capped so one very
#: large organisation cannot force an unbounded response. Row counts come from
#: aggregate queries and are exact; the HITL human-only breakdown is the one
#: exception, because it is derived from a bounded scan and flags itself as a
#: lower bound when that scan truncates.
_SAMPLE_LIMIT: Final[int] = 20

#: Bounded scan of decided HITL rows used to count human-only approvals. The
#: gate config is JSON, and the count is computed in Python so the query works
#: identically on Postgres and SQLite. If an organisation has more decided gates
#: than this, the entry reports the count as a LOWER BOUND
#: (``human_only_counts_are_lower_bounds``) rather than as an exact figure.
_HITL_SCAN_LIMIT: Final[int] = 10000

#: The decision vocabulary's approval outcomes (``ck_hitl_claims_decision`` in
#: ``db/models/hitl_claim.py``). ``deliver_manual`` supplies the output a human
#: wrote in place of the agent's, so it is an approval of the gate; counting only
#: ``approved`` would under-report human oversight. ``rejected`` is not an
#: approval.
APPROVAL_DECISIONS: Final[frozenset[str]] = frozenset({"approved", "deliver_manual"})


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
    # FAR-2055: the audit period this index covers, as half-open interval
    # ``[period_from, period_to)``. BOTH None means the org's FULL HISTORY -
    # the index was not scoped. The pair always travels with the response so a
    # scoped index can never be misread as a full-history one.
    period_from: str | None
    period_to: str | None
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


def normalise_audit_period(
    period_from: datetime | None,
    period_to: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Normalise and validate an audit-period pair.

    The interval is half-open, ``[period_from, period_to)``, so
    ``period_from == period_to`` is a valid (empty) window. Only a start AFTER
    the end is rejected. The route maps the raised :class:`ValueError` to 422;
    :func:`build_evidence_index` validates too, so the library surface fails
    the same way for any other caller. A naive bound (an ISO string without an
    offset) is read as UTC; an aware bound is left untouched.
    """
    start = _as_utc_aware(period_from)
    end = _as_utc_aware(period_to)
    if start is not None and end is not None and start > end:
        raise ValueError("the audit period start (from) must not be after its end (to)")
    return start, end


def _apply_period(
    stmt: Any,
    column: Any,
    period_from: datetime | None,
    period_to: datetime | None,
) -> Any:
    """Constrain *stmt* to the half-open period on *column*.

    ``[period_from, period_to)``: ``>= from`` and ``< to``. Each bound is
    optional; a bound of None simply omits that predicate, so an unscoped call
    applies nothing at all.
    """
    if period_from is not None:
        stmt = stmt.where(column >= period_from)
    if period_to is not None:
        stmt = stmt.where(column < period_to)
    return stmt


def _window_suffix(period_from: datetime | None, period_to: datetime | None) -> str:
    """Describe the audit window for an absence reason ('' when unscoped)."""
    if period_from is None and period_to is None:
        return ""
    legs: list[str] = []
    if period_from is not None:
        legs.append(f"on or after {_iso(period_from)}")
    if period_to is not None:
        legs.append(f"before {_iso(period_to)}")
    return " in the requested audit period (" + " and ".join(legs) + ")"


# ---------------------------------------------------------------------------
# Collectors - one per indexed artefact type
# ---------------------------------------------------------------------------


async def _collect_audit_chain(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
    *,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> ArtifactEntry:
    location = (
        "GET /api/v1/admin/audit (browse), GET /api/v1/admin/audit/verify (integrity), "
        "GET /api/v1/admin/audit/evidence-pack (signed offline-verifiable export)"
    )
    head = await get_chain_head(session, organisation_id)
    # Served by ix_audit_events_org_created_at (migration 0155): the count and
    # both min/max bounds are answered from one (organisation_id, created_at)
    # btree, with the period a range on its second column.
    stats_stmt = select(
        func.count(AuditEvent.id),
        func.min(AuditEvent.created_at),
        func.max(AuditEvent.created_at),
    ).where(AuditEvent.organisation_id == organisation_id)
    stats = (await session.execute(_apply_period(stats_stmt, AuditEvent.created_at, period_from, period_to))).one()
    event_count = int(stats[0] or 0)
    if event_count == 0:
        # No events in this window: the instance holds nothing to evidence for
        # the period. The reason names the period when one was requested - a
        # scoped absence must never read as "this org has no audit trail".
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_AUDIT_CHAIN,
            location=location,
            reason=(
                "this instance holds no audit events for the organisation"
                + (_window_suffix(period_from, period_to) or " yet")
            ),
            detail={"event_count": event_count},
        )
    if head is None:
        # Events exist but no chain head does. That is NOT "no events yet" -
        # saying so next to a positive event_count would be a provably false
        # reason, and it is a materially different finding: the trail exists but
        # its integrity cannot be evidenced.
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_AUDIT_CHAIN,
            location=location,
            reason=(
                "audit events exist for the organisation"
                + _window_suffix(period_from, period_to)
                + " but no audit chain head is recorded, so chain integrity cannot be evidenced"
            ),
            detail={"event_count": event_count, "chain_head_present": False},
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
            # The chain head is organisation-wide by definition; it is not
            # period-scoped. event_count IS the windowed count, so the two
            # numbers can legitimately differ on a scoped index and
            # event_count_scope states which is which.
            "chain_head_event_count": int(head.event_count),
            "event_count": event_count,
            "event_count_scope": (
                "organisation-wide full history"
                if period_from is None and period_to is None
                else "organisation-wide, limited to the requested audit period"
            ),
            "first_event_at": _iso(stats[1]),
            "last_event_at": _iso(stats[2]),
        },
    )


async def _collect_run_snapshots(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
    *,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> ArtifactEntry:
    location = "GET /api/v1/pipelines/{pipeline_id}/snapshots and the snapshot_id on GET /api/v1/runs/{run_id}"
    # Both legs are filtered here too: a run row can reference a snapshot id,
    # and the join makes the snapshot side reachable independently of the run.
    # The period is anchored on the RUN's created_at: the evidence is "a run
    # executed in the period against a frozen snapshot", and a snapshot may be
    # reused by runs in several periods.
    # Served by ix_runs_org_snapshot_id (migration 0326): a count(DISTINCT
    # snapshot_id) over the org is an index-only scan, no heap fetch per run.
    count_stmt = (
        select(func.count(func.distinct(Run.snapshot_id)))
        .join(PipelineSnapshot, PipelineSnapshot.id == Run.snapshot_id)
        .where(
            Run.organisation_id == organisation_id,
            PipelineSnapshot.organisation_id == organisation_id,
        )
    )
    snapshot_count = int(
        (await session.execute(_apply_period(count_stmt, Run.created_at, period_from, period_to))).scalar_one() or 0
    )
    if snapshot_count == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_RUN_SNAPSHOT,
            location=location,
            reason=(
                "no run in this instance has executed against a frozen pipeline snapshot"
                + _window_suffix(period_from, period_to)
            ),
        )

    # Bounded sample: pick the _SAMPLE_LIMIT most recently-run snapshots in a
    # LIMIT-ed subquery FIRST, then fetch their metadata by primary key. The
    # previous shape (DISTINCT over the whole join, then ORDER BY + LIMIT)
    # had to materialise every distinct snapshot before it could sort, so the
    # "limit" bounded the response but not the work. The subquery is served by
    # the (organisation_id, created_at) runs index.
    recent_runs = select(
        Run.snapshot_id.label("snapshot_id"),
        func.max(Run.created_at).label("last_run_at"),
    ).where(Run.organisation_id == organisation_id)
    recent_runs = _apply_period(recent_runs, Run.created_at, period_from, period_to)
    recent_snapshots = (
        recent_runs.group_by(Run.snapshot_id).order_by(func.max(Run.created_at).desc()).limit(_SAMPLE_LIMIT).subquery()
    )
    sample_stmt = (
        select(
            PipelineSnapshot.id,
            PipelineSnapshot.snapshot_version,
            PipelineSnapshot.pipeline_id,
            PipelineSnapshot.created_at,
        )
        .join(recent_snapshots, PipelineSnapshot.id == recent_snapshots.c.snapshot_id)
        # Both legs are filtered: the module's contract is that EVERY query
        # carries an organisation_id predicate, and the join makes the
        # snapshot side reachable independently of the run side.
        .where(PipelineSnapshot.organisation_id == organisation_id)
        .order_by(recent_snapshots.c.last_run_at.desc())
    )
    rows = (await session.execute(sample_stmt)).all()
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
    *,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> ArtifactEntry:
    location = (
        "GET /api/v1/hitl/reviews and GET /api/v1/runs/{run_id}/hitl/pending; the "
        "decision is stored on the HITL claim row"
    )
    # Both legs are served by ix_hitl_claims_org_decision_at (migration 0326):
    # (organisation_id, decision_at) WHERE decision IS NOT NULL. The pre-existing
    # per-actor window index leads on decided_by, which neither query filters.
    by_decision_stmt = select(HitlClaim.decision, func.count(HitlClaim.id)).where(
        HitlClaim.organisation_id == organisation_id,
        HitlClaim.decision.is_not(None),
    )
    by_decision = (
        await session.execute(
            _apply_period(by_decision_stmt, HitlClaim.decision_at, period_from, period_to).group_by(HitlClaim.decision)
        )
    ).all()
    counts = {str(row[0]): int(row[1]) for row in by_decision}
    total = sum(counts.values())
    if total == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_HITL_DECISION,
            location=location,
            reason=(
                "no human-in-the-loop gate has been decided in this instance" + _window_suffix(period_from, period_to)
            ),
        )

    decided_stmt = select(
        HitlClaim.id,
        HitlClaim.decision,
        HitlClaim.decision_at,
        HitlClaim.decided_by,
        HitlClaim.gate_config_json,
    ).where(
        HitlClaim.organisation_id == organisation_id,
        HitlClaim.decision.is_not(None),
    )
    decided_stmt = _apply_period(decided_stmt, HitlClaim.decision_at, period_from, period_to)
    # A scoped window already excludes NULL decision_at rows (NULL cannot
    # satisfy ``>= from``), so the backward index scan serves the DESC order
    # directly. Unscoped, ``nulls_last`` is explicit: Postgres sorts NULLs
    # FIRST under ``DESC``, which would put undated rows at the head of the
    # scan, report ``latest_decision_at`` as None and fill the truncated window
    # with rows that carry no decision time at all.
    decided_rows = (
        await session.execute(decided_stmt.order_by(HitlClaim.decision_at.desc().nulls_last()).limit(_HITL_SCAN_LIMIT))
    ).all()
    human_only_approved = 0
    human_only_total = 0
    for row in decided_rows:
        # Single-source the fail-safe default (FAR-609): an omitted, null or
        # non-dict ``human_only`` means TRUE, so a legacy/raw-JSON gate is
        # counted as human oversight, never skipped.
        if not human_only_effective(row[4]):
            continue
        human_only_total += 1
        if row[1] in APPROVAL_DECISIONS:
            human_only_approved += 1

    truncated = total > _HITL_SCAN_LIMIT
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
            # Both counts are derived from the bounded scan, so when the scan
            # truncated they are lower bounds on the true figures. The flag
            # states which they are; they are never presented as exact when
            # they are not.
            "human_only_gate_decisions": human_only_total,
            "human_only_gate_approvals": human_only_approved,
            "human_only_scan_truncated": truncated,
            "human_only_counts_are_lower_bounds": truncated,
            "human_only_approval_outcomes": sorted(APPROVAL_DECISIONS),
            "latest_decision_at": _iso(latest),
        },
    )


async def _collect_eval_results(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pack: MappingPack,
    *,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> ArtifactEntry:
    location = (
        "GET /api/v1/runs/{run_id}/evals and GET /api/v1/eval-suites; each result "
        "records the eval-definition version that scored it"
    )
    # The count/sum/max legs are served by ix_eval_results_org_evaluated_at
    # (migration 0218); count(DISTINCT eval_id) by ix_eval_results_org_eval_id
    # (migration 0326). Both are index-only for the org, and the period is a
    # range on the second column of the first.
    stats_stmt = select(
        func.count(EvalResult.id),
        func.sum(case((EvalResult.passed.is_(True), 1), else_=0)),
        func.max(EvalResult.evaluated_at),
        func.count(func.distinct(EvalResult.eval_id)),
    ).where(EvalResult.organisation_id == organisation_id)
    stats = (await session.execute(_apply_period(stats_stmt, EvalResult.evaluated_at, period_from, period_to))).one()
    total = int(stats[0] or 0)
    if total == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_EVAL_RESULT,
            location=location,
            reason=("no evaluation has been recorded in this instance" + _window_suffix(period_from, period_to)),
        )

    passed = int(stats[1] or 0)
    sample_stmt = select(EvalResult.id).where(EvalResult.organisation_id == organisation_id)
    sample_ids = (
        (
            await session.execute(
                _apply_period(sample_stmt, EvalResult.evaluated_at, period_from, period_to)
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
    *,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> ArtifactEntry:
    location = (
        "Audit events pipeline.autonomy_promotion_decided and pipeline.autonomy_demoted at GET /api/v1/admin/audit"
    )
    by_type_stmt = select(AuditEvent.event_type, func.count(AuditEvent.id)).where(
        AuditEvent.organisation_id == organisation_id,
        AuditEvent.event_type.in_(AUTONOMY_EVENT_TYPES),
    )
    by_type = (
        await session.execute(
            _apply_period(by_type_stmt, AuditEvent.created_at, period_from, period_to).group_by(AuditEvent.event_type)
        )
    ).all()
    counts = {str(row[0]): int(row[1]) for row in by_type}
    total = sum(counts.values())
    if total == 0:
        return _absent(
            pack=pack,
            artifact_type=ARTIFACT_AUTONOMY_CHANGE,
            location=location,
            reason=(
                "no pipeline autonomy level has been promoted or demoted in this instance"
                + _window_suffix(period_from, period_to)
            ),
        )

    sample_stmt = select(AuditEvent.id).where(
        AuditEvent.organisation_id == organisation_id,
        AuditEvent.event_type.in_(AUTONOMY_EVENT_TYPES),
    )
    sample_ids = (
        (
            await session.execute(
                _apply_period(sample_stmt, AuditEvent.created_at, period_from, period_to)
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
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> EvidenceIndex:
    """Build the per-instance evidence index for *organisation_id*.

    The caller supplies the session and is responsible for the transaction and
    RLS context (the route does this). Every query here ALSO filters on
    ``organisation_id`` explicitly, so the index cannot widen beyond the
    caller's tenant even if a future caller forgets the RLS context.

    ``period_from`` / ``period_to`` scope EVERY artefact aggregate to the
    half-open interval ``[period_from, period_to)``. Both unset (the default)
    yields the organisation's **full history** - the only behaviour before
    FAR-2055 - and the response records that with ``period_from``/
    ``period_to`` of None. Both bounds are normalised to an aware datetime
    (naive means UTC) and a start after the end raises :class:`ValueError`.

    ``strict=True`` raises :class:`EvidenceIndexIncompleteError` when a mapped
    artefact type has no records in the requested window. The default reports
    the same condition as an ``absent`` entry, which is loud in the payload
    without making the whole index unavailable.
    """
    period_from, period_to = normalise_audit_period(period_from, period_to)
    pack = load_mapping_pack()
    artifacts = (
        await _collect_audit_chain(session, organisation_id, pack, period_from=period_from, period_to=period_to),
        await _collect_run_snapshots(session, organisation_id, pack, period_from=period_from, period_to=period_to),
        await _collect_hitl_decisions(session, organisation_id, pack, period_from=period_from, period_to=period_to),
        await _collect_eval_results(session, organisation_id, pack, period_from=period_from, period_to=period_to),
        await _collect_autonomy_changes(session, organisation_id, pack, period_from=period_from, period_to=period_to),
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
        period_from=_iso(period_from),
        period_to=_iso(period_to),
        product_version=get_version(),
        organisation_id=str(organisation_id),
        framework_ids=pack.framework_ids(),
        coverage=pack.coverage_counts(),
        complete=not missing,
        missing_artifact_types=missing,
        artifacts=artifacts,
    )
