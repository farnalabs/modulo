"""OAuth 2.0 client model for MCP authorization code flow.

Each client belongs to an organisation and carries scopes
and allowed redirect URIs.
"""

import uuid

from sqlalchemy import ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class OAuthClient(OrgScoped):
    __tablename__ = "oauth_clients"
    __table_args__ = ({"comment": "OAuth 2.0 client credentials per organisation"},)

    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    client_secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, comment="Space-separated scope values")
    redirect_uris: Mapped[str] = mapped_column(Text, nullable=False, comment="Space-separated allowed redirect URIs")
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # FAR-1476: the client's team boundary. Mirrors ``OrgApiKey.team_id`` —
    # a non-null value scopes every token issued to this client to that team
    # (the MCP auth legs feed it into ``_ctx_team_id`` so the existing
    # team-scoped-credential machinery enforces it). NULL = an org-wide client
    # with no team boundary (the pre-FAR-1476 behaviour). ``runner``-registered
    # clients must never be NULL (registration enforces this).
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
