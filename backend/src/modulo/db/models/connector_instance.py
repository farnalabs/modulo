import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class ConnectorInstance(SoftDeleteMixin, OrgScoped):
    __tablename__ = "connector_instances"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_connector_instances_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_connector_instances_team_owner",
        ),
        CheckConstraint(
            "tier IN ('native', 'preview', 'in_dev')",
            name="ck_connector_instances_tier",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    connector_type_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"))
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    credentials_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    allowed_operations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="active")
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_check_error: Mapped[str | None] = mapped_column(String(2000))
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="native")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
