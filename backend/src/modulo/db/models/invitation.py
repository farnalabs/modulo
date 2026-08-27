"""One-time in-app invitation for org enrollment (FAR-461).

Admins mint an enrollment link instead of typing a temporary password: the
SHA-256 hex of a ``secrets.token_urlsafe(32)`` plaintext is stored here; the
plaintext is shown to the inviting admin exactly once (never persisted) and
embedded in ``<origin>/accept-invite?token=...``.

Deliberately NOT an :class:`OrgScoped` subclass — this table sits outside the
``rls_org_isolation`` regime because consumption happens on the
unauthenticated accept-invite route before any principal exists. Every access
path scopes by organisation explicitly (see db/crud/invitations.py).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, TimestampMixin


class Invitation(Base, TimestampMixin):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
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
