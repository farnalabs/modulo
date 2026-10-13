import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

# Repeated column type (S1192): JSON with PostgreSQL JSONB variant. Both graph
# columns are jsonb in the DB (migration 0147 standardised them); the PG
# variant keeps the ORM honest against the migrated schema while the generic
# JSON keeps SQLite parity (the repo-wide multi-backend convention).
_JSONB_COL = JSON().with_variant(JSONB(), "postgresql")


class CompositeTemplate(SoftDeleteMixin, OrgScoped):
    __tablename__ = "composite_templates"
    __table_args__ = (
        # Partial unique from migration 0156_add_soft_delete_partial_uniques —
        # one live template per (org, name); a soft-deleted row no longer blocks
        # a re-create. Declared on the model so the ORM metadata matches the DB.
        Index(
            "uq_composite_templates_org_name",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sub_pipeline_graph_json: Mapped[dict[str, Any]] = mapped_column(_JSONB_COL, nullable=False, default=dict)
    parameter_ports_json: Mapped[list[dict[str, Any]]] = mapped_column(_JSONB_COL, nullable=False, default=list)
    input_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    output_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parameter_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("parameter_schemas.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0.0")
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
