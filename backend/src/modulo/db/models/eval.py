"""Eval ORM model — scoring mechanism definition (FAR-1060, chunk 1).

Net-new table mirroring EvalDefinition minus failure_behaviour.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import JSONB_COL, OrgScoped, SoftDeleteMixin

# Centralised eval_type vocabulary — MUST match EvalDefinition's CHECK constraint
# vocabulary AND order exactly.  A test asserts consistency across both tables.
_VALID_EVAL_TYPES = (
    "model_judge",
    "regex",
    "json_schema",
    "custom_function",
    "guardrail",
    "human_set",
)

# Build the SQL fragment once so both CheckConstraints share a single source of truth.
_eval_type_sql = ", ".join(f"'{v}'" for v in _VALID_EVAL_TYPES)


class Eval(SoftDeleteMixin, OrgScoped):
    __tablename__ = "evals"
    __table_args__ = (
        CheckConstraint(
            f"eval_type IN ({_eval_type_sql})",
            name="ck_evals_type",
        ),
        CheckConstraint(
            "pass_threshold IS NULL OR pass_threshold BETWEEN 0 AND 1",
            name="ck_evals_pass_threshold",
        ),
        # FAR-2028: mute threshold must be between 0 and 1.
        CheckConstraint(
            "mute_threshold IS NULL OR mute_threshold BETWEEN 0 AND 1",
            name="ck_evals_mute_threshold",
        ),
        # FAR-2028: mute_min_outcomes must be positive when set.
        CheckConstraint(
            "mute_min_outcomes IS NULL OR mute_min_outcomes > 0",
            name="ck_evals_mute_min_outcomes",
        ),
        UniqueConstraint("id", "organisation_id", name="uq_evals_id_organisation_id"),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    pre_version_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FAR-644: no FK to nodes.id — ``nodes`` is the deprecated composite-template
    # store; these node ids are JSON-graph ids (pipelines.graph_nodes_json) that
    # never materialise as ``nodes`` rows (see migration 0170's correction), so
    # an enforced FK would reject every legitimate write.
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    suite_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    eval_suite_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("eval_suites.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    eval_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Physical jsonb: 0250 created it ``json``; 0254 promoted it to match the
    # 0147 standard. JSONB_COL keeps the ORM declaration honest while
    # preserving SQLite/MariaDB parity.
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB_COL, nullable=False, default=dict)
    pass_threshold: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # deleted_by exists but SoftDeleteMixin supplies deleted_at.
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    # FAR-2028: Cached acceptance rate (percentage of findings resolved as 'accepted').
    # Computed by the mute policy evaluator on each finding resolution.
    acceptance_rate: Mapped[float | None] = mapped_column(nullable=True)
    # FAR-2028: Mute policy fields.
    is_muted: Mapped[bool] = mapped_column(nullable=False, server_default="false", default=False)
    muted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    muted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # FAR-2028: Mute policy configuration.
    # When acceptance_rate < mute_threshold across mute_min_outcomes recorded outcomes,
    # the definition is auto-muted (stops interrupting but keeps recording).
    mute_threshold: Mapped[float | None] = mapped_column(nullable=True)
    mute_min_outcomes: Mapped[int | None] = mapped_column(Integer, nullable=True)
