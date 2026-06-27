"""OAuth 2.0 authorization codes and token families for MCP OAuth flow.

Authorization codes are short-lived and one-time-use.
Token families implement rotation detection (reuse pattern from user token_families).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class OAuthAuthorizationCode(Base):
    __tablename__ = "oauth_authorization_codes"
    __table_args__ = {"comment": "One-time authorization codes for OAuth 2.0 flow"}  # noqa: RUF012

    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scopes: Mapped[str] = mapped_column(Text, nullable=False, comment="Space-separated requested scopes")
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    code_challenge: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="PKCE S256 challenge")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OAuthTokenFamily(Base):
    """Token family for OAuth access token rotation detection.

    Mirrors the pattern in token_family.py but keyed by client_id
    instead of user_id.
    """

    __tablename__ = "oauth_token_families"
    __table_args__ = {"comment": "Token families for MCP OAuth access token rotation"}  # noqa: RUF012

    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    max_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    blacklisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
