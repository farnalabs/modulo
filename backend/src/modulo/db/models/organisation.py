import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class Organisation(Base):
    __tablename__ = "organisations"
    __table_args__ = (CheckConstraint("status IN ('active', 'suspended', 'deleted')", name="ck_organisations_status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # This is deliberately not an FK: the first organisation must exist before its first user.
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    otel_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    plan_id: Mapped[str | None] = mapped_column(String(255))
    daily_spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    deletion_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deletion_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    export_bundle_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
