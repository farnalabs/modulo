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
    ``metrics_dump`` SAQ cron job (FAR-1717): ``_aggregate_staged_events`` folds
    each day's rows into the payload's ``event_counts`` section, and
    ``_purge_staging`` deletes consumed rows (watermark past their date) plus
    any row older than the retention bound on every cron tick. Turning the org's
    level off also purges its staged rows immediately
    (``api/routes/product_analytics.py``).

    ``ix_metrics_staging_recorded_at`` (single column) serves the purge's
    global ``recorded_at < :cutoff`` DELETE; the composite
    ``ix_metrics_staging_org_recorded_at`` serves the per-org ingest reads and
    the opt-out purge.
    """

    __tablename__ = "metrics_staging"
    __table_args__ = (
        UniqueConstraint("organisation_id", "event_id", name="uq_metrics_staging_org_event_id"),
        Index("ix_metrics_staging_org_recorded_at", "organisation_id", "recorded_at"),
        Index("ix_metrics_staging_recorded_at", "recorded_at"),
    )

    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
