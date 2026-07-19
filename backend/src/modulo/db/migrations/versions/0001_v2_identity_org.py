"""v2 — Identity & Org foundation.

Creates accounts, organisations, org memberships, SSO, and OAuth tables
plus the cross-org foreign-key guard trigger function.

Revision ID: 0001_v2_identity_org
Revises: None
Create Date: 2026-07-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_v2_identity_org"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables that take the strict org-isolation RLS policy.
_STRICT_RLS: tuple[str, ...] = ("sso_providers",)

# Tables queried BEFORE org context is established (identity-bootstrap).
_NULL_CONTEXT_RLS: tuple[str, ...] = (
    "org_memberships",
    "oauth_authorization_codes",
    "oauth_token_families",
    "oauth_clients",
    "token_families",
)


def upgrade() -> None:
    _create_tables()
    _create_trigger_functions()
    _create_triggers()
    _enable_rls()


def _create_tables() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("auth_provider", sa.String(length=20), server_default="local", nullable=False),
        sa.Column("sso_subject", sa.String(length=512), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("preferences", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("is_system_admin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("auth_provider IN ('local', 'oidc', 'saml', 'scim')", name="ck_accounts_auth_provider"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "organisations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("otel_config_json", sa.JSON(), nullable=False),
        sa.Column("plan_id", sa.String(length=255), nullable=True),
        sa.Column("daily_spend_limit", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("deletion_token", sa.String(length=128), nullable=True),
        sa.Column("deletion_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("export_bundle_json", sa.JSON(), nullable=True),
        sa.CheckConstraint("status IN ('active', 'suspended', 'deleted')", name="ck_organisations_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "token_families",
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=True),
        sa.Column("max_sequence", sa.Integer(), nullable=False),
        sa.Column("is_blacklisted", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("blacklisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("family_id"),
    )
    op.create_index(op.f("ix_token_families_account_id"), "token_families", ["account_id"], unique=False)
    op.create_index(op.f("ix_token_families_organisation_id"), "token_families", ["organisation_id"], unique=False)
    op.create_table(
        "sso_providers",
        sa.Column("provider_type", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("client_id", sa.String(length=1024), nullable=True),
        sa.Column("client_secret", sa.LargeBinary(), nullable=True),
        sa.Column("discovery_url", sa.String(length=2048), nullable=True),
        sa.Column("metadata_url", sa.String(length=2048), nullable=True),
        sa.Column("metadata_xml", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.String(length=1024), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("auto_provision", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("default_role", sa.String(length=32), server_default="runner", nullable=False),
        sa.Column("group_mappings", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sso_providers_organisation_id"), "sso_providers", ["organisation_id"], unique=False)
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_secret_hash", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("redirect_uris", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_oauth_clients_client_id"), "oauth_clients", ["client_id"], unique=True)
    op.create_index(op.f("ix_oauth_clients_organisation_id"), "oauth_clients", ["organisation_id"], unique=False)
    op.create_table(
        "oauth_authorization_codes",
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index(
        op.f("ix_oauth_authorization_codes_client_id"), "oauth_authorization_codes", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_oauth_authorization_codes_organisation_id"),
        "oauth_authorization_codes",
        ["organisation_id"],
        unique=False,
    )
    op.create_table(
        "oauth_token_families",
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("max_sequence", sa.Integer(), nullable=False),
        sa.Column("is_blacklisted", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("blacklisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("family_id"),
    )
    op.create_index(op.f("ix_oauth_token_families_client_id"), "oauth_token_families", ["client_id"], unique=False)
    op.create_index(
        op.f("ix_oauth_token_families_organisation_id"), "oauth_token_families", ["organisation_id"], unique=False
    )
    op.create_table(
        "org_memberships",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), server_default="runner", nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'operator', 'runner', 'viewer')", name="ck_org_memberships_role"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "organisation_id", name="uq_org_memberships_account_org"),
    )
    op.create_index(op.f("ix_org_memberships_account_id"), "org_memberships", ["account_id"], unique=False)
    op.create_index(op.f("ix_org_memberships_organisation_id"), "org_memberships", ["organisation_id"], unique=False)


def _create_trigger_functions() -> None:
    op.execute(
        sa.text("""
        CREATE FUNCTION enforce_same_organisation() RETURNS trigger AS $$
        DECLARE
            referenced_id uuid;
            referenced_organisation_id uuid;
            child_organisation_id uuid;
        BEGIN
            referenced_id := (to_jsonb(NEW) ->> TG_ARGV[1])::uuid;
            IF referenced_id IS NULL THEN
                RETURN NEW;
            END IF;
            child_organisation_id := (to_jsonb(NEW) ->> 'organisation_id')::uuid;
            EXECUTE format('SELECT organisation_id FROM %I WHERE id = $1', TG_ARGV[0])
                INTO referenced_organisation_id USING referenced_id;
            IF referenced_organisation_id IS NULL OR referenced_organisation_id <> child_organisation_id
            THEN
                RAISE EXCEPTION 'cross-organisation reference from %.% to %',
                    TG_TABLE_NAME, TG_ARGV[1], TG_ARGV[0]
                    USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    )


_TENANT_REFS: tuple[tuple[str, str, str], ...] = (
    ("oauth_clients", "account_id", "accounts"),
    ("org_memberships", "account_id", "accounts"),
    ("token_families", "account_id", "accounts"),
)


def _create_triggers() -> None:
    for child_table, child_column, parent_table in _TENANT_REFS:
        op.execute(
            sa.text(
                f'CREATE TRIGGER "trg_{child_table}_{child_column}_tenant" '
                f'BEFORE INSERT OR UPDATE OF "{child_column}", "organisation_id" ON "{child_table}" '
                f"FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('{parent_table}', '{child_column}')"
            )
        )


def _enable_rls() -> None:
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    null_context = f"{strict} OR nullif(current_setting('app.organisation_id', true), '') IS NULL"

    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))
    for table in _NULL_CONTEXT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({null_context})'))


def downgrade() -> None:
    for table in _NULL_CONTEXT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    for child_table, child_column, _ in _TENANT_REFS:
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "trg_{child_table}_{child_column}_tenant" ON "{child_table}"'))

    op.execute(sa.text("DROP FUNCTION IF EXISTS enforce_same_organisation() CASCADE"))

    op.drop_index(op.f("ix_org_memberships_organisation_id"), table_name="org_memberships")
    op.drop_index(op.f("ix_org_memberships_account_id"), table_name="org_memberships")
    op.drop_table("org_memberships")
    op.drop_index(op.f("ix_oauth_token_families_organisation_id"), table_name="oauth_token_families")
    op.drop_index(op.f("ix_oauth_token_families_client_id"), table_name="oauth_token_families")
    op.drop_table("oauth_token_families")
    op.drop_index(op.f("ix_oauth_authorization_codes_organisation_id"), table_name="oauth_authorization_codes")
    op.drop_index(op.f("ix_oauth_authorization_codes_client_id"), table_name="oauth_authorization_codes")
    op.drop_table("oauth_authorization_codes")
    op.drop_index(op.f("ix_oauth_clients_organisation_id"), table_name="oauth_clients")
    op.drop_index(op.f("ix_oauth_clients_client_id"), table_name="oauth_clients")
    op.drop_table("oauth_clients")
    op.drop_index(op.f("ix_sso_providers_organisation_id"), table_name="sso_providers")
    op.drop_table("sso_providers")
    op.drop_index(op.f("ix_token_families_organisation_id"), table_name="token_families")
    op.drop_index(op.f("ix_token_families_account_id"), table_name="token_families")
    op.drop_table("token_families")
    op.drop_table("organisations")
    op.drop_table("accounts")
