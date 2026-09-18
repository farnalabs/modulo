"""EvalDataset / EvalCase — the repeatable input corpus for evals (FAR-375 Phase 2).

Phase 2 is the **data layer only**: a managed, versioned input corpus that an
eval suite (Phase 3) runs against. It deliberately contains NO run-execution,
endpoint, or UI logic — that is Phase 3's territory.

Design intent (the original "task-type breadth" gap, restated): an eval suite
must be able to re-run the *same* inputs repeatedly and compare results,
independent of any single Run's lifetime. So the corpus stores its OWN payload
(mirroring ``WebhookPayload.raw_payload``), decoupled from ``Run.input_payload``.
When Runs are pruned for retention, the eval inputs survive.

EvalCase content is **DATA-ONLY**. It is stored verbatim and returned verbatim;
it is never interpolated into a system prompt and never executed as a directive.
The structural enforcement that the payload cannot *become* instructions lives
at the eval boundary (LLM-judge + SUT paths) and is Phase 3's responsibility —
Phase 2 guarantees only storage-as-data. See ``test_eval_dataset.py`` for the
injection-style round-trip assertion.

Soft-delete (``deleted_at`` / ``deleted_by``) is the sole deletion path for both
entities: an ``EvalCase`` references its dataset with ``ON DELETE RESTRICT`` so a
dataset that still owns cases can never be hard-removed; a dataset itself is only
soft-deleted here (the "referenced by a SuiteRun" hard-delete guard lands in
Phase 3). Both tables carry org-scoped RLS (``rls_org_isolation``) owned by
``modulo_migrate`` so the app role cannot bypass isolation.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class EvalDataset(OrgScoped):
    """A named, versioned collection of eval input cases owned by an org (or team)."""

    __tablename__ = "eval_datasets"

    # Team-scoped visibility: an 'org' dataset is readable by the whole org;
    # a 'team' dataset is restricted to its owning team's members (Phase 3
    # enforces the team-membership gate — Phase 2 stores the field only).
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    visibility: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        server_default="org",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Bumped whenever the corpus contents change meaningfully; consumers pin the
    # version they validated against so a content edit never silently shifts a
    # contract (mirrors the human eval set versioning discipline).
    version: Mapped[int] = mapped_column(nullable=False, server_default="1")

    # Two-step soft-delete: stamp ``deleted_at``/``deleted_by`` instead of
    # hard-removing, so any snapshot pin / future reference keeps resolving to a
    # skipped-with-audit path rather than a dangling row. A second admin-only
    # purge step (see ``purge_soft_deleted_eval_cases``) actually removes
    # soft-deleted rows.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)

    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_eval_datasets_visibility"),
        # One active name per org; soft-deleted datasets free their name for reuse.
        Index(
            "uq_eval_datasets_org_name_active",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class EvalCase(OrgScoped):
    """A single repeatable input for an eval dataset — stored DATA-ONLY, verbatim.

    ``input_payload`` is the canonical payload store (mirrors
    ``WebhookPayload.raw_payload``): it is persisted exactly as supplied and
    returned exactly as stored. ``expected_output`` is an optional reference
    answer used by Phase 3's scoring; it too is data-only. ``input_hash`` is a
    SHA-256 of ``input_payload`` for dedupe and audit trail.
    """

    __tablename__ = "eval_cases"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        # RESTRICT: a dataset that still owns cases can never be hard-deleted,
        # even via cascade — the corpus integrity must be explicit.
        ForeignKey("eval_datasets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expected_output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)

    __table_args__ = (
        # One active case per (dataset, payload-hash): identical inputs are not
        # stored twice. Soft-deleted cases release their hash for re-add.
        Index(
            "uq_eval_cases_dataset_hash_active",
            "dataset_id",
            "input_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


def compute_input_hash(input_payload: dict[str, Any]) -> str:
    """SHA-256 of the input payload, deterministic across Python versions.

    Serialised with ``sort_keys`` + stable separators so semantically identical
    payloads produce the same hash regardless of key ordering in the source.
    """
    import hashlib
    import json

    canonical = json.dumps(input_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_dataset_has_cases(session: Any, dataset_id: uuid.UUID) -> int:
    """Return the count of active (non-soft-deleted) cases for a dataset.

    Phase 3 run-execution calls this to refuse empty datasets at run time: a
    dataset with zero active cases is a no-op / warning, never a silent pass.
    Returns 0 for an empty dataset; the caller treats 0 as "nothing to run".
    """
    from sqlalchemy import func, select

    return (
        session.scalar(
            select(func.count())
            .select_from(EvalCase)
            .where(EvalCase.dataset_id == dataset_id, EvalCase.deleted_at.is_(None))
        )
        or 0
    )


def purge_soft_deleted_eval_cases(session: Any, older_than: datetime) -> int:
    """Housekeeping: hard-delete eval_cases soft-deleted before ``older_than``.

    Cases have no dependents, so this is the safe retention-pruning step for the
    payload store. (Dataset hard-delete/purge is intentionally NOT provided here:
    an ``EvalCase.dataset_id`` FK with ``ON DELETE RESTRICT`` already forbids
    removing a referenced dataset; the "referenced by a SuiteRun" guard is
    Phase 3's concern.) Returns the number of rows removed.
    """
    from sqlalchemy import delete

    result = session.execute(delete(EvalCase).where(EvalCase.deleted_at.is_not(None), EvalCase.deleted_at < older_than))
    return result.rowcount or 0
