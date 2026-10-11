"""One-time in-app invitation for org enrollment (FAR-461).

Admins mint an enrollment link instead of typing a temporary password: the
SHA-256 hex of a ``secrets.token_urlsafe(32)`` plaintext is stored here; the
plaintext is shown to the inviting admin exactly once (never persisted) and
embedded in ``<origin>/accept-invite#token=...``.

Deliberately NOT an :class:`OrgScoped` subclass — this table sits outside the
``rls_org_isolation`` regime because consumption happens on the
unauthenticated accept-invite route before any principal exists. Every access
path scopes by organisation explicitly (see db/crud/invitations.py).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, TimestampMixin

# Partial-index liveness predicate (un-consumed, un-revoked) — mirrors the
# shared _live_conditions scope in db/crud/invitations.py and 0291's
# _LIVE_WHERE (S1192: referenced by both partial indexes below).
_LIVE_WHERE = "consumed_at IS NULL AND revoked_at IS NULL"


class Invitation(Base, TimestampMixin):
    __tablename__ = "invitations"
    __table_args__ = (
        # Live-invite lookup for (organisation, email): backs
        # get_live_for_email / has_live_for_email (invite-duplicate guard
        # and the SSO join gate, which runs on every SSO login). The
        # pre-existing single-column organisation_id index cannot serve
        # the email equality; the partial predicate matches the shared
        # _live_conditions liveness scope (un-consumed, un-revoked).
        Index(
            "ix_invitations_org_email_live",
            "organisation_id",
            "email",
            postgresql_where=text(_LIVE_WHERE),
            sqlite_where=text(_LIVE_WHERE),
        ),
        # Live-invite expiry scan: every liveness check filters
        # expires_at > now() and a stale-invite purge sweeps on it.
        Index(
            "ix_invitations_expires_at_live",
            "expires_at",
            postgresql_where=text(_LIVE_WHERE),
            sqlite_where=text(_LIVE_WHERE),
        ),
        # Role vocabulary is otherwise enforced only at the API boundary
        # (admin.invite-user); crud.create_invitation does not validate.
        CheckConstraint(
            "org_role IN ('admin', 'operator', 'runner', 'viewer')",
            name="ck_invitations_org_role",
        ),
        # token_hash is always a SHA-256 hex digest (see hash_token).
        # length() is used over char_length() so the expression parses
        # on both Postgres and SQLite (unit-test create_all).
        CheckConstraint(
            "length(token_hash) = 64",
            name="ck_invitations_token_hash_len",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    org_role: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
