from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.organisation import Organisation


class User(OrgScoped):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("org_role IN ('admin', 'operator', 'runner', 'viewer')", name="ck_users_org_role"),
        CheckConstraint("auth_provider IN ('local', 'oidc', 'saml')", name="ck_users_auth_provider"),
        UniqueConstraint("organisation_id", "email", name="uq_users_organisation_email"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    org_role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="runner")
    auth_provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default="local")
    sso_subject: Mapped[str | None] = mapped_column(String(512))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default=sa_text("'{}'::json"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    organisation: Mapped[Organisation] = relationship()
