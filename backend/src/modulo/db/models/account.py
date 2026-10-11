import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, String, Uuid
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, TimestampMixin

# ---------------------------------------------------------------------------
# Principal kind (ADR 056 / ADR 058, FAR-1385 slice 1).
#
# Every actor in Modulo is a principal. ``kind`` classifies one as a human
# person (``human``) or a non-human service principal (``service``). The
# discriminator is a column on ``accounts`` (ADR 058 decided column, not a
# parallel table), NOT NULL with a ``'human'`` server default so the migration
# is byte-identical for every pre-existing row.
#
# Service rows carry NO human-shaped identity: ``email``, ``auth_provider``,
# ``sso_subject``, ``password_hash`` and ``last_login`` are all NULL. That is
# the structural reason no login path can reach one — a service row has no
# email to look up, no password to verify and no SSO subject to match. The
# two CHECK constraints below make the remaining two human-only privileges
# (system admin, break-glass) impossible for a service row at the DB level.
# ---------------------------------------------------------------------------
KIND_HUMAN = "human"
KIND_SERVICE = "service"

#: The complete, ordered kind vocabulary (mirrors ``ck_accounts_kind``).
ACCOUNT_KINDS: tuple[str, ...] = (KIND_HUMAN, KIND_SERVICE)


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("auth_provider IN ('local', 'oidc', 'saml', 'scim')", name="ck_accounts_auth_provider"),
        CheckConstraint(
            "NOT is_break_glass OR break_glass_expires_at IS NOT NULL OR break_glass_deactivated_at IS NOT NULL",
            name="ck_accounts_break_glass_expiry",
        ),
        CheckConstraint("kind IN ('human', 'service')", name="ck_accounts_kind"),
        # A service principal can never be a system admin or a break-glass
        # account (ADR 058: "machine principals never hold admin (for now)").
        # Enforced in the database, not only in application code, so the rule
        # cannot be bypassed by a future write path that forgets to check.
        CheckConstraint(
            "kind = 'human' OR (NOT is_system_admin AND NOT is_break_glass)",
            name="ck_accounts_service_privilege",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4, server_default=sa_text("gen_random_uuid()")
    )
    # ``email`` is NULLABLE at the DB level (FAR-1385): a service principal has
    # no human-shaped identity, so no login/invite/SSO/SCIM/audit lookup can
    # reach one by email, and the UNIQUE constraint tolerates any number of
    # NULLs. The ORM annotation stays ``str`` deliberately: every code path that
    # reads ``email`` is a human-only path, and a service row cannot exist until
    # slice 2 ships a mint surface — so the annotation is the human invariant,
    # while the DB column (nullable=True, pinned by a test) is the storage
    # shape. A reader of a service row must never assume this value is present.
    email: Mapped[str] = mapped_column(String(320), nullable=True, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=KIND_HUMAN)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # NULL on service rows (no auth provider). The CHECK constraint admits
    # NULL (a NULL comparison is UNKNOWN, never FALSE).
    auth_provider: Mapped[str | None] = mapped_column(String(20), nullable=True, server_default="local")
    sso_subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, server_default=sa_text("'{}'"))
    is_system_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_break_glass: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    break_glass_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    break_glass_deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
