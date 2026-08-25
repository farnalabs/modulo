import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.error_event import ErrorEvent


class ErrorGroup(OrgScoped):
    __tablename__ = "error_groups"

    __table_args__ = (
        UniqueConstraint("organisation_id", "fingerprint", name="uq_error_groups_org_fingerprint"),
        CheckConstraint("status IN ('new', 'acknowledged', 'resolved', 'archived')", name="ck_error_groups_status"),
        CheckConstraint("level_peak IN ('error', 'warning', 'critical')", name="ck_error_groups_level_peak"),
    )

    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="new")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    level_peak: Mapped[str] = mapped_column(String(20), nullable=False, server_default="error")
    sample_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("error_events.id", ondelete="SET NULL"), index=True
    )
    sample_event: Mapped[Optional["ErrorEvent"]] = relationship(
        "ErrorEvent", foreign_keys=[sample_event_id], lazy="joined"
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
