import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Index, String, Uuid, func
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, TimestampMixin


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("auth_provider IN ('local', 'oidc', 'saml', 'scim')", name="ck_accounts_auth_provider"),
        CheckConstraint(
            "NOT is_break_glass OR break_glass_expires_at IS NOT NULL OR break_glass_deactivated_at IS NOT NULL",
            name="ck_accounts_break_glass_expiry",
        ),
        # FAR-584: emails are case-insensitive. Case-insensitive uniqueness is
        # enforced by this functional unique index (migration 0176 created the
        # DB-side twin after backfilling + collision-guarding existing rows);
        # the plain case-sensitive UNIQUE constraint on email is retired.
        Index("uq_accounts_email_lower", func.lower(sa_text("email")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default="local")
    sso_subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default=sa_text("'{}'"))
    is_system_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_break_glass: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    break_glass_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    break_glass_deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
