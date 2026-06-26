import uuid
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class PipelineEdge(OrgScoped):
    __tablename__ = "pipeline_edges"
    __table_args__ = (
        CheckConstraint("edge_type IN ('normal', 'reject')", name="ck_pipeline_edges_type"),
        UniqueConstraint(
            "pipeline_id",
            "source_node_id",
            "target_node_id",
            "edge_type",
            name="uq_pipeline_edges_path",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    target_node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(10), nullable=False, server_default="normal")
    hitl_gate_config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
