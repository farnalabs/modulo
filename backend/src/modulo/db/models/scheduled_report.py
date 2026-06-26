from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class ScheduledReport(OrgScoped):
    __tablename__ = "scheduled_reports"

    period: Mapped[str] = mapped_column(String(20), nullable=False)
    group_by: Mapped[str] = mapped_column(String(20), nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False, server_default="csv")
    recipients: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="one_time")
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=False
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
