"""``mcp_servers`` — the customer-side MCP servers their agents consume (FAR-1692 S1/S2).

The reverse direction from the existing Modulo-as-server surface
(``mcp_setup_tokens``, ``api/routes/mcp_oauth.py``): rows here describe MCP
servers the CUSTOMER runs that external agents connect to. This is **policy
substrate and a record, not enforcement** (ADR 055): Modulo cannot police a
client it does not control, so the honest posture is *detect and surface*.

It is deliberately a dedicated table rather than a ``ConnectorInstance``
(routed more, FAR-1692 Decision 1(a)): an MCP server has a distinct lifecycle —
an observed tool-definition set and a pin/approval state — that the dispatch
connector contract does not carry. Credentials, where held, are Fernet-encrypted
at rest via the shared ``modulo.auth.secret_storage.encrypt_stored_secret`` helper
so at-rest crypto does not diverge.

**Tool-definition pinning (S2).** ``tools_json`` is the last observed tool set;
``tools_hash`` is its canonical hash (``core.mcp.tool_pinning``). Approval
records the hash that was approved (``approved_hash`` + ``approved_by`` /
``approved_at``). On re-observation, a hash that differs from ``approved_hash``
flips ``approval_status`` to ``approved-stale`` and raises an operator
notification + audit event — it never auto-blocks (the client is outside
Modulo's control). ``approval_status`` is a first-class readable field so
policies/evals can branch on the pin state (ADR 055 §4).

Scoping: org-scoped with a team-visibility pair, DB-RLS-enforced
(``rls_team_isolation``, migration ``0314``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import JSONB_COL, ONDELETE_SET_NULL, OrgScoped, SoftDeleteMixin

_FK_ACCOUNTS_ID = "accounts.id"

# Approval lifecycle vocabulary (also enforced by ck_mcp_servers_approval_status).
APPROVAL_STATUS_UNAPPROVED = "unapproved"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_APPROVED_STALE = "approved-stale"
APPROVAL_STATUS_BLOCKED = "blocked"

APPROVAL_STATUSES: tuple[str, ...] = (
    APPROVAL_STATUS_UNAPPROVED,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_APPROVED_STALE,
    APPROVAL_STATUS_BLOCKED,
)

TRANSPORTS: tuple[str, ...] = ("stdio", "sse", "http", "websocket")


class McpServer(SoftDeleteMixin, OrgScoped):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_mcp_servers_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_mcp_servers_team_owner",
        ),
        CheckConstraint("transport IN ('stdio', 'sse', 'http', 'websocket')", name="ck_mcp_servers_transport"),
        CheckConstraint(
            "approval_status IN ('unapproved', 'approved', 'approved-stale', 'blocked')",
            name="ck_mcp_servers_approval_status",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # How Modulo reaches the server: a remote transport (http/sse/websocket)
    # carries ``endpoint``; a local ``stdio`` server carries ``command``.
    transport: Mapped[str] = mapped_column(String(30), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(2000))
    command: Mapped[str | None] = mapped_column(String(2000))
    declared_scopes: Mapped[list[str]] = mapped_column(JSONB_COL, nullable=False, default=list)
    owner_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_FK_ACCOUNTS_ID, ondelete=ONDELETE_SET_NULL), index=True
    )
    # Optional Fernet ciphertext for a server-side credential (a remote MCP
    # server's bearer token, say). NEVER exposed in a response — only
    # ``has_credentials``. Same at-rest crypto as ``connector_instances``.
    credentials_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)

    # --- Tool-definition pinning (FAR-1692 S2) ---
    tools_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB_COL, nullable=False, default=list)
    tools_hash: Mapped[str | None] = mapped_column(String(64))
    approval_status: Mapped[str] = mapped_column(String(30), nullable=False, server_default=APPROVAL_STATUS_UNAPPROVED)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_FK_ACCOUNTS_ID, ondelete=ONDELETE_SET_NULL)
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
