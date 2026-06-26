from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.environment_profile import EnvironmentProfile


class WorkspaceLease(OrgScoped):
    __tablename__ = "workspace_leases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'provisioning', 'active', 'completed', 'failed', 'expired')",
            name="ck_workspace_leases_status",
        ),
    )

    environment_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("environment_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resource_usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    environment_profile: Mapped[EnvironmentProfile] = relationship()
