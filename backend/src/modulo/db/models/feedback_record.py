import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class FeedbackRecord(OrgScoped):
    __tablename__ = "feedback_records"
    __table_args__ = (
        CheckConstraint(
            "feedback_status IN ('pending', 'routing', 'correcting', 'resolved', 'escalated', 'dismissed')",
            name="ck_feedback_records_status",
        ),
        CheckConstraint(
            "feedback_handler_type IN ('human', 'ai_correction', 'ai_correction_with_human_review')",
            name="ck_feedback_records_handler_type",
        ),
        # Exact-match hot lookup for (org, run, node) in
        # ``core/feedback_manager/queries.get_feedback_record_for_node``
        # (migration 0303).
        Index(
            "ix_feedback_records_org_run_node",
            "organisation_id",
            "run_id",
            "producing_node_id",
        ),
        # Tenant list + dashboard trend: WHERE org [+ status] ORDER BY
        # created_at DESC (migration 0303). The (organisation_id,
        # created_at) prefix also serves the unfiltered sweep via a btree
        # backward scan.
        Index(
            "ix_feedback_records_org_status_created",
            "organisation_id",
            "feedback_status",
            "created_at",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=False)
    # JSONB on Postgres (migration 0147 promoted this column from json);
    # generic JSON keeps SQLite/MariaDB parity (the evidence.py pattern).
    rejected_output: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    producing_node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    producing_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    feedback_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    feedback_handler_type: Mapped[str] = mapped_column(String(40), nullable=False, server_default="human")
    correction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    eval_gap: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    needs_human_review: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    annotation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSONB on Postgres (migration 0112 created this column as JSONB);
    # generic JSON keeps SQLite/MariaDB parity (the evidence.py pattern).
    correction_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
