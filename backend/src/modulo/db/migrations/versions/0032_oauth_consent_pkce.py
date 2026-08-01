"""OAuth account-bound codes + PKCE + consent-state store (ADR 017 A1b, migration 2).

Revision ID: 0032_oauth_consent_pkce
Revises: 0030_drop_owner_role_add_authz_enforce
Create Date: 2026-08-01

The pre-A1b flow minted anonymous authorization codes bound to no consenting
account, accepted PKCE without validating it, and had no consent/state store.
This migration:

1. Deletes all existing ``oauth_authorization_codes`` — they were minted
   anonymously (no account_id, no consenting user), are one-time and
   10-minute TTL, so zero value is lost.
2. Makes ``account_id`` NOT NULL on ``oauth_authorization_codes`` (a code is
   only minted by the authenticated approve endpoint from now on).
3. Adds ``code_challenge_method`` (S256 only) — the challenge is now verified
   at token time per RFC 7636.
4. Creates ``oauth_consent_states`` — the single-use, TTL-bounded browser
   consent handoff created by the anonymous authorize 302 and consumed by the
   authenticated approve POST.

Fully revertible: the downgrade drops the new columns/table (ADR 017 A1b).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_oauth_consent_pkce"
down_revision: str | None = "0030_drop_owner_role_add_authz_enforce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRICT_RLS: tuple[str, ...] = ("oauth_consent_states",)


def upgrade() -> None:
    # 1. Anonymous codes are worthless and unsafe — purge before making
    #    account_id NOT NULL (the code was minted with no consenting user).
    op.execute("DELETE FROM oauth_authorization_codes")

    # 2. Every code minted from now on is bound to the account that approved
    #    the consent (ADR 017 DECISION 1 — approve POST is the consent).
    op.add_column(
        "oauth_authorization_codes",
        sa.Column("account_id", sa.Uuid(), nullable=False),
    )
    op.create_foreign_key(
        "fk_oauth_authorization_codes_account_id",
        "oauth_authorization_codes",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_oauth_authorization_codes_account_id",
        "oauth_authorization_codes",
        ["account_id"],
    )

    # 3. PKCE method is pinned to S256; the challenge is now verified at token
    #    exchange (RFC 7636) instead of being accepted and ignored.
    op.add_column(
        "oauth_authorization_codes",
        sa.Column(
            "code_challenge_method",
            sa.String(8),
            nullable=False,
            server_default="S256",
        ),
    )

    # 4. Consent-state store: created by the anonymous authorize GET, populated
    #    with the account_id at approve, single-use via UPDATE ... RETURNING.
    op.create_table(
        "oauth_consent_states",
        sa.Column("state", sa.String(128), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("redirect_uri", sa.String(1024), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="TTL ~15 min — authorize-created consent handoff",
        ),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "account_id",
            sa.Uuid(),
            nullable=True,
            comment="Populated at approve from the Bearer principal",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_oauth_consent_states_organisation_id",
        "oauth_consent_states",
        ["organisation_id"],
    )

    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))


def downgrade() -> None:
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
    op.drop_table("oauth_consent_states")

    op.drop_constraint("fk_oauth_authorization_codes_account_id", "oauth_authorization_codes", type_="foreignkey")
    op.drop_index("ix_oauth_authorization_codes_account_id", table_name="oauth_authorization_codes")
    op.drop_column("oauth_authorization_codes", "code_challenge_method")
    op.drop_column("oauth_authorization_codes", "account_id")
