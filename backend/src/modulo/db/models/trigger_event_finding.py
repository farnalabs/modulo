"""Runless guardrail findings attached to a trigger-event intake decision."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class TriggerEventFinding(OrgScoped):
    """One guardrail finding for a delivery that may have no pipeline run."""

    __tablename__ = "trigger_event_findings"
    __table_args__ = (
        CheckConstraint(
            "resolution IN ('accepted', 'dismissed', 'superseded', 'pending')",
            name="ck_trigger_event_findings_resolution",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    trigger_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("trigger_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    eval_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("evals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    passed: Mapped[bool] = mapped_column(nullable=False)
    outcome: Mapped[str] = mapped_column(Text(), nullable=False)
    score: Mapped[float | None] = mapped_column(nullable=True)
    detail: Mapped[str | None] = mapped_column(Text(), nullable=True)
    resolution: Mapped[str] = mapped_column(Text(), nullable=False, server_default="pending")
    resolution_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
