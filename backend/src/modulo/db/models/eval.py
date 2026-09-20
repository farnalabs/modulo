"""Eval ORM model — scoring mechanism definition (FAR-1060, chunk 1).

Net-new table mirroring EvalDefinition minus failure_behaviour.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, Numeric, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

# Centralised eval_type vocabulary — MUST match EvalDefinition's CHECK constraint
# vocabulary AND order exactly.  A test asserts consistency across both tables.
_VALID_EVAL_TYPES = (
    "llm_judge",
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
        CheckConstraint(
            "id = id OR organisation_id = organisation_id",
            name="uq_evals_id_organisation_id",
        ),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    pre_version_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Deliberately NOT a FK — this is a JSON-graph node id (pipelines.graph_nodes_json),
    # same as EvalDefinition.node_id.  See EvalDefinition.node_id comment for rationale.
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
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    pass_threshold: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # deleted_by exists but SoftDeleteMixin supplies deleted_at.
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
