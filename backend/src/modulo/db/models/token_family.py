"""Token family model for refresh token rotation and family invalidation."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base
from modulo.db.models.organisation import ORPHAN_ORG_ID


class TokenFamily(Base):
    __tablename__ = "token_families"

    family_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Migration 0234 makes organisation_id NOT NULL and backfills NULLs to the
    # orphan-organisation sentinel; the server_default keeps inserts safe when no
    # org is resolved (e.g. system-admin logins).
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        server_default=text(f"'{ORPHAN_ORG_ID}'"),
    )
    max_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    blacklisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
