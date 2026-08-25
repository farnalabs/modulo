import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class EvalDefinition(OrgScoped):
    __tablename__ = "eval_definitions"
    __table_args__ = (
        CheckConstraint(
            "eval_type IN ('llm_judge', 'regex', 'json_schema', 'custom_function', 'guardrail', 'human_set')",
            name="ck_eval_definitions_type",
        ),
        CheckConstraint(
            "failure_behaviour IN ('warn', 'block')",
            name="ck_eval_definitions_failure_behaviour",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    eval_type: Mapped[str] = mapped_column(String(30), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    failure_behaviour: Mapped[str] = mapped_column(String(10), nullable=False, server_default="warn")
    pass_threshold: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    suite_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # First-class grouping (FAR-374 Phase 1). Backfilled from ``suite_id`` by
    # migration 0126. ``suite_id`` is retained (present but deprecated) as the
    # legacy source of truth for read-back during the transition; new code must
    # resolve grouping through ``eval_suite_id``. Do NOT write to both in
    # parallel — that creates a dual source of truth.
    eval_suite_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("eval_suites.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Eval-definition versioning (FAR-382): each definition carries an integer
    # ``version`` starting at 1, bumped on every create and update so a rubric
    # change is an explicitly version-scoped event. ``pre_version_raw`` snapshots
    # the raw config as it existed BEFORE the current version was stamped, so a
    # reversal is reconstructable from the column pair rather than lost. It is
    # NULL for definitions that have never been updated since versioning.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    pre_version_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Two-step soft-delete (FAR-309 PR B): a guardrail eval definition is
    # SOFT-deleted (``deleted_at``/``deleted_by`` stamped) instead of hard
    # removed, so snapshot pins that reference it keep resolving to a
    # skipped-with-audit path rather than a dangling row. A second,
    # admin-only purge step actually removes soft-deleted rows. Live binding
    # (``load_pipeline_guardrail_rows``) excludes soft-deleted rows.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
