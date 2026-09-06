"""org_api_keys gains the ``scope`` column (FAR-620 Phase 1).

Revision ID: 0178_org_api_keys_scope
Revises: 0177_invitations
Create Date: 2026-09-06

``scope`` classifies an API key's caller scope on the existing scope axis
(org-wide / team-scoped / per-run):

* ``'org'`` — org-level caller (the historical default; every key minted
  before this column existed reads ``'org'`` via the server_default).
* ``'user'`` — per-user key: the key operates as its creator's identity and
  may reach caller-scoped (``.self``) MCP tools. Minting is gated by the
  ``user_scoped_mcp_keys`` org feature flag and a per-account active-key
  quota (10); the column is IMMUTABLE post-mint (update payloads reject it).

The CHECK mirrors ``ck_org_api_keys_role``. DDL is portable (plain
``ADD COLUMN`` with an inline CHECK — no ``ADD CONSTRAINT``, which SQLite
does not support), so the unit round-trip harness runs it on SQLite and
Postgres alike. The downgrade drops exactly the column the upgrade added
(silent widening: former user-scoped keys read ``'org'`` after a downgrade —
pinned as accepted in the FAR-620 spec; re-enabling the flag re-broadens,
never the downgrade).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0178_org_api_keys_scope"
down_revision: str | None = "0177_invitations"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "org_api_keys",
        sa.Column(
            "scope",
            sa.String(length=10),
            # Inline so the ADD COLUMN statement is portable across Postgres
            # and SQLite (SQLite cannot attach a table-level CHECK to an
            # added column, and Postgres accepts an inline CHECK in
            # ADD COLUMN).
            sa.CheckConstraint("scope IN ('org', 'user')", name="ck_org_api_keys_scope"),
            nullable=False,
            server_default="org",
        ),
    )


def downgrade() -> None:
    op.drop_column("org_api_keys", "scope")
