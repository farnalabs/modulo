from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class ScheduledReport(OrgScoped):
    __tablename__ = "scheduled_reports"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    report_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    config_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True, default=None)
    recipient_config: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True, default=None)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def period(self) -> str:
        value = (self.config_json or {}).get("period", "monthly")
        return value if isinstance(value, str) else "monthly"

    @property
    def group_by(self) -> str:
        value = (self.config_json or {}).get("group_by", "team")
        return value if isinstance(value, str) else "team"

    @property
    def format(self) -> str:
        value = (self.config_json or {}).get("format", "csv")
        return value if isinstance(value, str) else "csv"

    @property
    def schedule_type(self) -> str:
        value = (self.config_json or {}).get("schedule_type", "recurring")
        return value if isinstance(value, str) else "recurring"

    @property
    def recipients(self) -> list[str]:
        value = (self.recipient_config or {}).get("emails", [])
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    @property
    def next_run_at(self) -> datetime | None:
        return self.next_send_at
