import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class TriggerEvent(OrgScoped):
    __tablename__ = "trigger_events"
    __table_args__ = (
        CheckConstraint(
            "validation_result IN ('accepted', 'passed', 'hmac_failed', "
            "'schema_validation_failed', 'deduplicated', 'concurrency_limit_reached', "
            "'flood_rejected', 'timestamp_expired', 'validation_failed', 'rate_limited', "
            "'no_match', 'condition_met', 'poll_error')",
            name="ck_trigger_events_validation_result",
        ),
    )

    trigger_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("triggers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    validation_result: Mapped[str] = mapped_column(String(50), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("runs.id", ondelete="SET NULL"))
    error_detail: Mapped[str | None] = mapped_column(String(2000))
