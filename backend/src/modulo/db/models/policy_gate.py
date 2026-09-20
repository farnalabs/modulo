"""PolicyGate ORM model — decision layer (FAR-1060, chunk 1).

Net-new table.  Absorbs EvalDefinition.failure_behaviour.
"""

import uuid
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class PolicyGate(SoftDeleteMixin, OrgScoped):
    __tablename__ = "policy_gates"
    __table_args__ = (
        CheckConstraint(
            "action IN ('warn', 'block')",
            name="ck_policy_gates_action",
        ),
        ForeignKeyConstraint(
            ["eval_id", "organisation_id"],
            ["evals.id", "evals.organisation_id"],
            ondelete="CASCADE",
            name="fk_policy_gates_eval_org",
        ),
        # Partial unique index: at most one live gate per Eval.  Created in the
        # migration as a Postgres partial index (WHERE deleted_at IS NULL); the
        # ORM declaration here records the semantic intent but the actual index
        # is a migration concern (partial indexes are not declarable in ORM
        # __table_args__ without dialect-specific kwargs).
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    pre_version_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    eval_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    # Deliberately NOT a FK — denormalised copy of the referenced Eval's node_id.
    # JSON-graph identifier (pipelines.graph_nodes_json), not a materialised row.
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    # deleted_by exists but SoftDeleteMixin supplies deleted_at.
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
