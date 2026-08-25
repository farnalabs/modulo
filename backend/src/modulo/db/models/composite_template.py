import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class CompositeTemplate(SoftDeleteMixin, OrgScoped):
    __tablename__ = "composite_templates"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sub_pipeline_graph_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    parameter_ports_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
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
