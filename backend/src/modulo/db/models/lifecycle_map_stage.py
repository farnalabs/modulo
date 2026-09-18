import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class LifecycleMapStage(OrgScoped):
    """A journey/map-stage projection row derived from ``lifecycle_maps.content_json``.

    This table is a READ projection, NOT a second source of truth — the
    canonical map content lives in ``lifecycle_maps.content_json``. Rows are
    replaced wholesale every time a map version is saved, so ``content_json``
    always wins on the next save.

    ``pipeline_id`` may appear in at most one non-soft-deleted map: the partial
    unique index ``uq_lifecycle_map_stages_active_pipeline`` enforces it at the
    DB layer, and the service performs a friendly pre-check before writing.
    """

    __tablename__ = "lifecycle_map_stages"
    __table_args__ = (
        CheckConstraint(
            "stage_type IN ('modulo', 'external', 'manual', 'placeholder')",
            name="ck_lifecycle_map_stages_type",
        ),
        UniqueConstraint("map_id", "version", "stage_id", name="uq_lifecycle_map_stages_map_version_stage"),
        Index(
            "uq_lifecycle_map_stages_active_pipeline",
            "organisation_id",
            "pipeline_id",
            unique=True,
            postgresql_where=text("pipeline_id IS NOT NULL"),
        ),
    )

    map_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("lifecycle_maps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    stage_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    stage_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipelines.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
