import uuid
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.environment_profile import EnvironmentProfile
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin


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
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
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
    composite_bindings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=True, default=list)
    parameter_bindings_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Guardrail snapshot pin (FAR-223 item 10): the guardrail set that was
    # bound at snapshot creation, serialized by ``serialize_guardrail_pin``.
    # Replays evaluate the PINNED set (the ORIGINAL conditions), not the live
    # rows; a pinned guardrail whose live row is gone is skipped-with-audit +
    # enforcement-gap alert. Mirrors the ``composite_bindings_json`` shape.
    guardrail_pins_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True, default=list)
    # Run-start snapshot-integrity fingerprint (FAR-309 PR B): canonical SHA-256
    # over the serialized ``guardrail_pins_json`` captured at snapshot creation.
    # The replay seam re-computes the fingerprint of the LOADED pins and fails
    # closed on a mismatch — a tampered/drifted pin set must never silently
    # change which guardrails evaluate. Nullable: legacy snapshots predating
    # the fingerprint are still trusted (verified only when present).
    guardrail_pins_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    default_autonomy_level: Mapped[str | None] = mapped_column(String(30))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    run_context_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    organisation: Mapped["Organisation"] = relationship()
    pipeline: Mapped["Pipeline"] = relationship()
    environment_profile: Mapped[Optional["EnvironmentProfile"]] = relationship()
    schema_pins: Mapped[list["SnapshotSchemaPin"]] = relationship(back_populates="snapshot")
