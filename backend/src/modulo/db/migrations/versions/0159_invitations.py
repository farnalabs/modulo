"""invitations — one-time in-app invite tokens (FAR-461).

Revision ID: 0159_invitations
Revises: 0158_accounts_must_change_password
Create Date: 2026-08-27

Admins hand new members a one-time enrollment link instead of typing a
temporary password for them (Modulo is self-hosted; outbound email is
optional and often absent, so the link is surfaced IN-APP via the admin
credential dialog). Each row stores the SHA-256 hex of a
``secrets.token_urlsafe(32)`` plaintext — the plaintext itself is shown to
the inviting admin exactly once and never persisted.

RLS NOTE — deliberately OUTSIDE the ``rls_org_isolation`` regime: unlike
``token_families`` / ``mcp_setup_tokens``, NO row-level-security policy is
created for this table. Invitation consumption happens on the unauthenticated
``POST /api/v1/auth/accept-invite`` route *before* any principal exists, so
there is no caller org context to satisfy an RLS predicate; enforcing RLS
here would make self-enrollment impossible. The table carries an index on
``organisation_id`` and every access path scopes by organisation explicitly
(admin routes scope to the caller's org; consumption routes by the unique
pre-hashed token). The mirror-side protections are unchanged: accounts and
org_memberships keep their existing RLS/ownership regimes, and the plaintext
token has 256 bits of entropy so enumeration of unscoped rows is not a
practical attack surface.

The migration is idempotent, following the guarded style used throughout
recent revisions (information_schema gates on upgrade, IF EXISTS on downgrade).
"""

from __future__ import annotations

from alembic import op

revision: str = "0159_invitations"
down_revision: str | None = "0158_accounts_must_change_password"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_UPGRADE_SQL = (
    "DO $$ BEGIN "
    "IF NOT EXISTS (SELECT 1 FROM information_schema.tables "
    "WHERE table_schema='public' AND table_name='invitations') THEN "
    "CREATE TABLE public.invitations ("
    "id UUID NOT NULL DEFAULT gen_random_uuid() CONSTRAINT pk_invitations PRIMARY KEY, "
    "organisation_id UUID NOT NULL REFERENCES public.organisations(id) ON DELETE CASCADE, "
    "email VARCHAR(320) NOT NULL, "
    "display_name VARCHAR(255) NOT NULL, "
    "org_role VARCHAR(20) NOT NULL, "
    "token_hash VARCHAR(64) NOT NULL UNIQUE, "
    "invited_by UUID NOT NULL REFERENCES public.accounts(id) ON DELETE RESTRICT, "
    "expires_at TIMESTAMP WITH TIME ZONE NOT NULL, "
    "consumed_at TIMESTAMP WITH TIME ZONE, "
    "revoked_at TIMESTAMP WITH TIME ZONE, "
    "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP"
    "); "
    "END IF; "
    "END $$;"
)

_UPGRADE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_invitations_organisation_id ON public.invitations (organisation_id);"
)

_DOWNGRADE_SQL = "DROP TABLE IF EXISTS public.invitations;"


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)
    op.execute(_UPGRADE_INDEX_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
