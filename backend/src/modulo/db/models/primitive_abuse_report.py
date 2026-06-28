"""PrimitiveAbuseReport model — abuse report queue for library ratings."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class PrimitiveAbuseReport(OrgScoped):
    """Reports of abusive/inappropriate library primitive ratings."""

    __tablename__ = "primitive_abuse_reports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'reviewed', 'dismissed')",
            name="ck_abuse_reports_status",
        ),
    )

    primitive_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("library_primitives.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("primitive_ratings.id", ondelete="SET NULL"),
        nullable=True,
    )
    reporter_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
