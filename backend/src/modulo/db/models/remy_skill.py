import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class RemySkill(Base):
    __tablename__ = "remy_skills"
    __table_args__ = (
        CheckConstraint(
            "(organisation_id IS NOT NULL AND account_id IS NULL) OR "
            "(organisation_id IS NULL AND account_id IS NOT NULL)",
            name="ck_remy_skills_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    triggers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    source_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
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
