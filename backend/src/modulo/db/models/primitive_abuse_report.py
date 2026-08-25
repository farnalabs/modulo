"""PrimitiveAbuseReport model — abuse report queue for library ratings."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid
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
        Uuid(), ForeignKey("primitive_ratings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reporter_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
