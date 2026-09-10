"""VariantBatchState — lightweight persistence for variant batch metadata (FAR-775)."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from modulo.db.models.organisation import Organisation


class VariantBatchState(Base, TimestampMixin):
    """Lightweight persistence row for a variant batch.

    Batches predating FAR-775 have no stored row — the route synthesizes
    detail/list entries from the runs themselves. When a row IS stored, it
    carries the batch name, pipeline/variant-group linkage, frozen input
    payload, and soft-delete support (deleted_at).
    """

    __tablename__ = "variant_batch_state"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
    )
    variant_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
    )
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        server_default="{}",
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    organisation: Mapped["Organisation"] = relationship()
