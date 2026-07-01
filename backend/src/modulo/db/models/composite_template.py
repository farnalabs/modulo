import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class CompositeTemplate(OrgScoped):
    __tablename__ = "composite_templates"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sub_pipeline_graph_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    parameter_ports_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    input_schema_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    output_schema_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0.0")
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False,
    )
