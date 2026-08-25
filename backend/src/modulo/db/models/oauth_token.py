"""OAuth 2.0 authorization codes, consent states and token families.

Authorization codes are short-lived, one-time-use, and bound to the account
that approved the browser consent (ADR 017 A1b). PKCE S256 challenges are
stored alongside the code and verified at token exchange (RFC 7636).

``oauth_consent_states`` is the single-use handoff created by the anonymous
authorize 302 and consumed by the authenticated approve POST.
Token families implement rotation detection (reuse pattern from user token_families).
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base

# FK target for the owning organisation (repeated across the OAuth tables).
_FK_ORGANISATIONS_ID = "organisations.id"


class OAuthAuthorizationCode(Base):
    __tablename__ = "oauth_authorization_codes"
    __table_args__ = ({"comment": "One-time authorization codes for OAuth 2.0 flow"},)

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey(_FK_ORGANISATIONS_ID, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Account that approved the consent (ADR 017 — approve POST is the consent)",
    )
    scopes: Mapped[str] = mapped_column(Text, nullable=False, comment="Space-separated requested scopes")
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    code_challenge: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="PKCE S256 challenge")
    code_challenge_method: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="S256", comment="PKCE method — S256 only"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )


class OAuthConsentState(Base):
    """Single-use, TTL-bounded browser consent handoff (ADR 017 A1b).

    Created by the anonymous authorize 302 (account_id NULL — the browser is
    not yet authenticated against the SPA). The authenticated approve POST
    validates it (single-use, unexpired, same org), populates ``account_id``
    from the Bearer principal, mints the code from the state row's scopes and
    challenge ONLY, marks it consumed, and returns the server-derived redirect
    URL. ``state`` is a client-chosen correlation/replay-binding nonce, NOT an
    anti-CSRF token — the Bearer requirement IS the consent-CSRF control.
    """

    __tablename__ = "oauth_consent_states"
    __table_args__ = ({"comment": "Single-use OAuth browser consent handoff states"},)

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, comment="Requested scopes — authoritative at mint")
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False, comment="PKCE S256 challenge")
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey(_FK_ORGANISATIONS_ID, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="TTL ~15 min")
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        comment="Populated at approve from the Bearer principal",
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )


class OAuthTokenFamily(Base):
    """Token family for OAuth access token rotation detection.

    Mirrors the pattern in token_family.py but keyed by client_id
    instead of user_id.
    """

    __tablename__ = "oauth_token_families"
    __table_args__ = ({"comment": "Token families for MCP OAuth access token rotation"},)

    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey(_FK_ORGANISATIONS_ID, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    max_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    blacklisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
