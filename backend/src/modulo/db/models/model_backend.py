import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.enums import ModelBackendProvider
from modulo.db.models.base import OrgScoped

_PROVIDER_VALUES = sorted(m.value for m in ModelBackendProvider)
_PROVIDER_SQL = f"provider IN ({', '.join(repr(v) for v in _PROVIDER_VALUES)})"


class ModelBackend(OrgScoped):
    __tablename__ = "model_backends"
    __table_args__ = (
        CheckConstraint(_PROVIDER_SQL, name="ck_model_backends_provider"),
        CheckConstraint("cost_tracking IN ('enabled', 'disabled')", name="ck_model_backends_cost"),
        CheckConstraint("visibility IN ('org', 'team')", name="ck_model_backends_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_model_backends_team_owner",
        ),
        CheckConstraint(
            "tier IN ('native', 'preview', 'in_dev')",
            name="ck_model_backends_tier",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    default_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cost_tracking: Mapped[str] = mapped_column(String(10), nullable=False, server_default="enabled")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="active")
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_check_error: Mapped[str | None] = mapped_column(String(2000))
    fallback_backend_ids: Mapped[list[uuid.UUID] | None] = mapped_column(JSON, nullable=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="native")
