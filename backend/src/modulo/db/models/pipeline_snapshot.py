from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.environment_profile import EnvironmentProfile
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline


class PipelineSnapshot(OrgScoped):
    __tablename__ = "pipeline_snapshots"
    __table_args__ = (UniqueConstraint("pipeline_id", "snapshot_version", name="uq_pipeline_snapshot_version"),)

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("users.id", ondelete="SET NULL"))
    environment_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("environment_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    graph_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    connector_bindings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    schema_pins_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    prompt_pins_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    model_backend_pins_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    tag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    default_autonomy_level: Mapped[str | None] = mapped_column(String(30))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    run_context_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    organisation: Mapped[Organisation] = relationship()
    pipeline: Mapped[Pipeline] = relationship()
    environment_profile: Mapped[EnvironmentProfile | None] = relationship()
