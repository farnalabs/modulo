import uuid

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped


class SnapshotSchemaPin(OrgScoped):
    __tablename__ = "snapshot_schema_pins"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    schema_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schemas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)

    __table_args__ = (
        CheckConstraint("direction IN ('input', 'output')", name="ck_snapshot_schema_pins_direction"),
        ForeignKeyConstraint(
            ["schema_id", "schema_version", "organisation_id"],
            ["schema_versions.schema_id", "schema_versions.version", "schema_versions.organisation_id"],
            ondelete="RESTRICT",
        ),
    )

    snapshot = relationship("PipelineSnapshot", back_populates="schema_pins")
