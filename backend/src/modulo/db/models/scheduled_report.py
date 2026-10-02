import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

# Repeated column type (S1192): JSON with PostgreSQL JSONB variant (the
# codebase JSON standard — migration 0147 promoted both JSON columns on
# this table to jsonb; the variant keeps the SQLite/MariaDB backends on
# generic JSON).
_JSONB_COL = JSON().with_variant(JSONB(), "postgresql")

# Repeated FK reference (S1192).
_FK_ACCOUNTS_ID = "accounts.id"
_ONDELETE_SET_NULL = "SET NULL"


class ScheduledReport(SoftDeleteMixin, OrgScoped):
    __tablename__ = "scheduled_reports"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    report_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    config_json: Mapped[dict[str, object] | None] = mapped_column(_JSONB_COL, nullable=True, default=None)
    recipient_config: Mapped[dict[str, object] | None] = mapped_column(_JSONB_COL, nullable=True, default=None)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # server_default mirrors Trigger.active: raw-SQL/rolling-deploy inserts
    # that omit ``active`` must not trip the NOT NULL constraint.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Audit columns (added to the DB by migration 0132 alongside deleted_at
    # and the (organisation_id, deleted_at) partial index; declared here so
    # ORM metadata matches the shipped contract and the SQLite mirror).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_FK_ACCOUNTS_ID, ondelete=_ONDELETE_SET_NULL)
    )
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_FK_ACCOUNTS_ID, ondelete=_ONDELETE_SET_NULL)
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
