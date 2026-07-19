import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.environment_profile import EnvironmentProfile


class WorkspaceLease(OrgScoped):
    __tablename__ = "workspace_leases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'expired')",
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
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    repository_url: Mapped[str | None] = mapped_column(String(1000))
    repository_ref: Mapped[str | None] = mapped_column(String(255))
    lease_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resource_usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_artifact_refs_json: Mapped[list[str] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)

    environment_profile: Mapped["EnvironmentProfile"] = relationship()
