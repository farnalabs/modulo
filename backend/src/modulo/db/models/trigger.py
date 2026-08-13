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

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class Trigger(SoftDeleteMixin, OrgScoped):
    __tablename__ = "triggers"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing')",
            name="ck_triggers_type",
        ),
        CheckConstraint("max_concurrent_runs > 0", name="ck_triggers_max_concurrent_runs"),
        # Ongoing triggers (FAR-158) are cost-controlled at the DB level: a daily
        # spend limit is REQUIRED (non-null, > 0) and the target pool size
        # ``max_concurrent_runs`` is bounded 1..20. Both are partial CHECKs — they
        # only apply to ``trigger_type = 'ongoing'`` rows — mirroring migration
        # 0092_ongoing_trigger_type.
        CheckConstraint(
            "trigger_type <> 'ongoing' OR (daily_spend_limit IS NOT NULL AND daily_spend_limit > 0)",
            name="ck_triggers_ongoing_spend_limit",
        ),
        CheckConstraint(
            "trigger_type <> 'ongoing' OR (max_concurrent_runs BETWEEN 1 AND 20)",
            name="ck_triggers_ongoing_target_range",
        ),
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
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    # Cron-specific fields (nullable for non-cron trigger types)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cron_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
