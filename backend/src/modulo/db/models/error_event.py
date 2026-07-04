from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class ErrorEvent(OrgScoped):
    __tablename__ = "error_events"

    __table_args__ = (
        CheckConstraint("level IN ('error', 'warning', 'critical')", name="ck_error_events_level"),
        CheckConstraint("source IN ('backend', 'frontend', 'celery')", name="ck_error_events_source"),
        CheckConstraint("status IN ('new', 'acknowledged', 'resolved', 'archived')", name="ck_error_events_status"),
    )

    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stacktrace: Mapped[str | None] = mapped_column(Text)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    environment: Mapped[str | None] = mapped_column(String(50))
    version: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="new")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
