import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Trigger(OrgScoped):
    __tablename__ = "triggers"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal')",
            name="ck_triggers_type",
        ),
        CheckConstraint("max_concurrent_runs > 0", name="ck_triggers_max_concurrent_runs"),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    daily_spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Cron-specific fields (nullable for non-cron trigger types)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cron_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
