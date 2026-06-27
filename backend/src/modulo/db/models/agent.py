import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, ForeignKeyConstraint, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


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
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    input_schema_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    input_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    output_schema_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    output_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    model_backend_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("model_backends.id", ondelete="RESTRICT"), nullable=False
    )
    connector_type_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    required_environment_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evals: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True, default=None)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    token_budget: Mapped[int | None] = mapped_column(Integer)
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("library_primitives.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
