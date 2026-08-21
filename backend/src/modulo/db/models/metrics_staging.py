"""MetricsStaging model for product analytics event staging."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class MetricsStaging(OrgScoped):
    """Staging table for product analytics events before daily dump.

    Events are inserted by the ingest endpoint and consumed by the daily
    ``metrics_dump`` SAQ cron job.  Rows older than the last successful
    dump are purged.
    """

    __tablename__ = "metrics_staging"
    __table_args__ = (
        UniqueConstraint("organisation_id", "event_id", name="uq_metrics_staging_org_event_id"),
        Index("ix_metrics_staging_org_recorded_at", "organisation_id", "recorded_at"),
    )

    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
