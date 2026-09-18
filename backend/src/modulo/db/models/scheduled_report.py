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
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    @property
    def period(self) -> str | None:
        return self._cost_config_string("period")

    @property
    def group_by(self) -> str | None:
        return self._cost_config_string("group_by")

    @property
    def format(self) -> str | None:
        return self._cost_config_string("format")

    @property
    def schedule_type(self) -> str | None:
        return self._cost_config_string("schedule_type")

    @property
    def recipients(self) -> list[str]:
        if self.report_type != "cost":
            return []
        value = (self.recipient_config or {}).get("emails", [])
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    @property
    def next_run_at(self) -> datetime | None:
        return self.next_send_at

    def _cost_config_string(self, key: str) -> str | None:
        if self.report_type != "cost":
            return None
        value = (self.config_json or {}).get(key)
        return value if isinstance(value, str) else None
