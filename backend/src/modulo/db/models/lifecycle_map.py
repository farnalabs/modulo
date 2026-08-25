import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class LifecycleMap(SoftDeleteMixin, OrgScoped):
    __tablename__ = "lifecycle_maps"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_lifecycle_maps_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_lifecycle_maps_team_owner",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_lifecycle_maps_version",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
