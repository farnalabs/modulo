import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, ForeignKeyConstraint, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class Agent(SoftDeleteMixin, OrgScoped):
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
    )

    is_executable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    prompt_always_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    template_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    agent_command: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)
    agent_commands: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=None)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    input_schema_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    input_schema_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    output_schema_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    output_schema_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    model_backend_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("model_backends.id", ondelete="RESTRICT"), nullable=True
    )
    connector_type_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    required_environment_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evals: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True, default=None)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    max_input_length: Mapped[int | None] = mapped_column(Integer)
    token_budget: Mapped[int | None] = mapped_column(Integer)
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("library_primitives.id", ondelete="SET NULL")
    )
    parameter_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("parameter_schemas.id", ondelete="RESTRICT"), nullable=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
