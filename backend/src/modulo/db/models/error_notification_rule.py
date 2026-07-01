from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class ErrorNotificationRule(OrgScoped):
    __tablename__ = "error_notification_rules"

    __table_args__ = (
        CheckConstraint("condition_level IN ('error', 'warning', 'critical')", name="ck_enr_condition_level"),
        CheckConstraint("action_type IN ('in_app', 'email', 'webhook')", name="ck_enr_action_type"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    condition_level: Mapped[str] = mapped_column(String(20), nullable=False, server_default="error")
    condition_min_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    condition_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    action_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="in_app")
    webhook_url: Mapped[str | None] = mapped_column(Text)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )
