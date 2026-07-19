import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, OrgScoped


class Notification(OrgScoped):
    __tablename__ = "notifications"

    __table_args__ = (
        CheckConstraint("scope IN ('user', 'org', 'admin')", name="ck_notifications_scope"),
        CheckConstraint("level IN ('debug', 'info', 'warning', 'error')", name="ck_notifications_level"),
        CheckConstraint(
            "dismiss_strategy IN ('user_only', 'org_admin', 'any_scope')",
            name="ck_notifications_dismiss_strategy",
        ),
    )

    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    action_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    dismiss_strategy: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="user_only",
    )
    dismissible_at_scope: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Dismissal(Base):
    __tablename__ = "dismissals"

    __table_args__ = (
        CheckConstraint("dismiss_scope IN ('self', 'scope')", name="ck_dismissals_scope"),
        UniqueConstraint("notification_id", "dismissed_by_user_id", name="uq_dismissal_user_notification"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dismissed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    dismiss_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
