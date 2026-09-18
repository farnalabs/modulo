"""WebVitalEvent model for frontend performance metrics."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class WebVitalEvent(OrgScoped):
    """Captures a single Web Vitals measurement from the frontend."""

    __tablename__ = "web_vital_events"

    __table_args__ = (
        CheckConstraint("metric_value >= 0", name="ck_web_vital_events_metric_value_nonneg"),
        # Time-range + org queries (metrics dashboard filters by org + recorded_at
        # window, optionally metric_name). OrgScoped already indexes
        # organisation_id alone; this composite supports the range scan.
        Index("ix_web_vital_events_org_recorded", "organisation_id", "recorded_at"),
    )

    metric_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    metric_value: Mapped[float] = mapped_column(Float(), nullable=False)
    metric_rating: Mapped[str | None] = mapped_column(String(20), nullable=True)
    route_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    navigation_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
