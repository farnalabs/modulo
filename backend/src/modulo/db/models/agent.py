import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped

# Repeated column type (S1192): JSON with PostgreSQL JSONB variant.
_JSONB_COL = JSON().with_variant(JSONB(), "postgresql")


class Agent(OrgScoped):
    __tablename__ = "agents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["input_schema_id", "input_schema_version", "organisation_id"],
            [
                "schema_versions.schema_id",
                "schema_versions.version",
                "schema_versions.organisation_id",
            ],
            name="fk_agents_input_schema_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["output_schema_id", "output_schema_version", "organisation_id"],
            [
                "schema_versions.schema_id",
                "schema_versions.version",
                "schema_versions.organisation_id",
            ],
            name="fk_agents_output_schema_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("token_budget IS NULL OR token_budget > 0", name="ck_agents_token_budget"),
        CheckConstraint("max_input_length IS NULL OR max_input_length > 0", name="ck_agents_max_input_length"),
        UniqueConstraint("organisation_id", "name", name="uq_agents_organisation_name"),
    )

    is_executable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    prompt_always_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("composite_templates.id", ondelete="SET NULL"), nullable=True, default=None
    )
    agent_commands: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True, default=None
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    input_schema_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    input_schema_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    output_schema_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    output_schema_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version_history: Mapped[list[dict[str, Any]]] = mapped_column(_JSONB_COL, nullable=False, default=list)
    model_backend_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("model_backends.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    connector_type_refs: Mapped[list[dict[str, Any]]] = mapped_column(_JSONB_COL, nullable=False, default=list)
    required_environment_capabilities: Mapped[list[str]] = mapped_column(_JSONB_COL, nullable=False, default=list)
    evals: Mapped[list[dict[str, Any]] | None] = mapped_column(_JSONB_COL, nullable=True, default=None)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(_JSONB_COL, nullable=False, default=dict)
    max_input_length: Mapped[int | None] = mapped_column(Integer)
    token_budget: Mapped[int | None] = mapped_column(Integer)
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("library_primitives.id", ondelete="SET NULL"), index=True
    )
    parameter_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("parameter_schemas.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    collection_install_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("collection_install.install_id", ondelete="SET NULL"), nullable=True, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
