import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class RemyContextSource(Base):
    __tablename__ = "remy_context_sources"
    __table_args__ = (
        CheckConstraint(
            "(organisation_id IS NOT NULL AND user_id IS NULL) OR (organisation_id IS NULL AND user_id IS NOT NULL)",
            name="ck_remy_context_sources_owner",
        ),
        CheckConstraint(
            "source_mode IN ('always_on', 'tool', 'off')",
            name="ck_remy_context_sources_mode",
        ),
        UniqueConstraint(
            "organisation_id",
            "user_id",
            "source_key",
            name="uq_remy_context_sources_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="always_on")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )
