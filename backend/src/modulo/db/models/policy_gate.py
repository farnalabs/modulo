"""PolicyGate ORM model — decision layer (FAR-1060, chunk 1).

Net-new table.  Absorbs EvalDefinition.failure_behaviour.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class PolicyGate(SoftDeleteMixin, OrgScoped):
    __tablename__ = "policy_gates"
    __table_args__ = (
        CheckConstraint(
            "action IN ('warn', 'block')",
            name="ck_policy_gates_action",
        ),
        # FAR-967 chunk 10 (§4.3) — symmetric operator-control CHECK, shipped
        # by migration 0272. The predicate and name match that migration's
        # SQLite batch definition EXACTLY so metadata.create_all() / SQLite-
        # mirror CTAS parity holds against the shipped post-0272 contract:
        # enabled ⟹ enabled_at NOT NULL AND disabled_at NULL;
        # NOT enabled ⟹ disabled_at NOT NULL AND enabled_at NULL.
        CheckConstraint(
            "(enabled AND enabled_at IS NOT NULL AND disabled_at IS NULL) "
            "OR "
            "(NOT enabled AND disabled_at IS NOT NULL AND enabled_at IS NULL)",
            name="ck_policy_gates_enabled_timestamps",
        ),
        ForeignKeyConstraint(
            ["eval_id", "organisation_id"],
            ["evals.id", "evals.organisation_id"],
            ondelete="CASCADE",
            name="fk_policy_gates_eval_org",
        ),
        UniqueConstraint("id", "organisation_id", name="uq_policy_gates_id_organisation_id"),
        Index(
            "uq_policy_gates_eval_id_live",
            "eval_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    pre_version_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    eval_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    # Deliberately NOT a FK — denormalised copy of the referenced Eval's node_id.
    # JSON-graph identifier (pipelines.graph_nodes_json), not a materialised row.
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    # FAR-967 operator safety control (chunk 10, s4.3): enabled/disabled
    # toggle with symmetric audit trail.  enabled => enabled_at NOT NULL
    # AND disabled_at NULL; NOT enabled => disabled_at NOT NULL AND
    # enabled_at NULL (ck_policy_gates_enabled_timestamps).
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)
    # FAR-967 §4.3 creation semantics: a freshly-inserted gate is ENABLED with
    # ``enabled_at`` stamped at creation, so the symmetric CHECK
    # (ck_policy_gates_enabled_timestamps) is satisfied by construction on
    # EVERY ORM insert path — including ``eval_definition_write``'s
    # Eval+PolicyGate persistence, which sets no audit columns itself.
    # Toggles write both timestamps explicitly on UPDATE, where defaults do
    # not apply (CO-2).
    enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(UTC),
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # deleted_by exists but SoftDeleteMixin supplies deleted_at.
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
