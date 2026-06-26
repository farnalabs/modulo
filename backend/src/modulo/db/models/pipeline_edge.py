import uuid
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class PipelineEdge(OrgScoped):
    __tablename__ = "pipeline_edges"
    __table_args__ = (
        CheckConstraint(
            "edge_type IN ('normal', 'reject', 'conditional')",
            name="ck_pipeline_edges_type",
        ),
        UniqueConstraint(
            "pipeline_id",
            "source_node_id",
            "target_node_id",
            "edge_type",
            name="uq_pipeline_edges_path",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(15), nullable=False, server_default="normal")
    hitl_gate_config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    condition_expression: Mapped[str | None] = mapped_column(String(500), nullable=True)
