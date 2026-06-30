# ruff: noqa: S608 — SQL f-strings in migration use hardcoded table/column names

"""Account + OrgMembership models, replacing User.

Creates ``accounts`` (global) and ``org_memberships`` (org-scoped) tables,
migrates existing ``users`` rows into both, drops all 29 FK constraints
pointing to ``users.id``, renames columns to ``account_id``, recreates FKs
pointing to ``accounts.id``, then drops the ``users`` table.

Revision ID: 0045_account_org_membership
Revises: 0044_library_auto_update
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_account_org_membership"
down_revision: str | Sequence[str] | None = "0044_library_auto_update"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# All FK references from child tables to `users.id`.
# (table, old_column, new_column, on_delete)
FK_SPECS: list[tuple[str, str, str, str]] = [
    ("token_families", "user_id", "account_id", "CASCADE"),
    ("hitl_claims", "claimed_by", "account_id", "SET NULL"),
    ("audit_events", "actor_user_id", "account_id", "SET NULL"),
    ("team_memberships", "user_id", "account_id", "CASCADE"),
    ("teams", "created_by", "account_id", "RESTRICT"),
    ("pipelines", "created_by", "account_id", "RESTRICT"),
    ("stages", "created_by", "account_id", "RESTRICT"),
    ("runs", "created_by", "account_id", "SET NULL"),
    ("agents", "created_by", "account_id", "RESTRICT"),
    ("schemas", "created_by", "account_id", "RESTRICT"),
    ("schema_versions", "created_by", "account_id", "RESTRICT"),
    ("triggers", "created_by", "account_id", "RESTRICT"),
    ("saved_views", "created_by", "account_id", "NO ACTION"),
    ("nodes", "created_by", "account_id", "RESTRICT"),
    ("node_categories", "created_by", "account_id", "RESTRICT"),
    ("model_backends", "created_by", "account_id", "RESTRICT"),
    ("connector_instances", "owner_id", "account_id", "RESTRICT"),
    ("eval_definitions", "created_by", "account_id", "RESTRICT"),
    ("org_api_keys", "created_by", "account_id", "RESTRICT"),
    ("library_primitives", "created_by", "account_id", "SET NULL"),
    ("environment_profiles", "created_by", "account_id", "SET NULL"),
    ("feedback_records", "rejected_by", "account_id", "RESTRICT"),
    ("notification_endpoints", "created_by", "account_id", "SET NULL"),
    ("node_observations", "human_observed_by", "account_id", "SET NULL"),
    ("oauth_clients", "created_by", "account_id", "SET NULL"),
    ("primitive_ratings", "user_id", "account_id", "SET NULL"),
    ("primitive_abuse_reports", "reporter_user_id", "reporter_account_id", "SET NULL"),
    ("primitive_abuse_reports", "reviewed_by", "reviewer_account_id", "SET NULL"),
    ("pipeline_snapshots", "created_by", "account_id", "SET NULL"),
    ("scheduled_reports", "created_by", "account_id", "SET NULL"),
]


def _drop_fk_for_column(table: str, column: str) -> None:
    """Drop any FK constraint on ``table`` that references ``users.id`` via ``column``."""
    op.execute(
        f"""
        DO $$
        DECLARE
            cons_name text;
        BEGIN
            SELECT con.conname INTO cons_name
            FROM pg_catalog.pg_constraint con
            JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid
            JOIN pg_catalog.pg_attribute att
                ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey)
            WHERE rel.relname = '{table}'
              AND con.contype = 'f'
              AND con.confrelid = (SELECT oid FROM pg_catalog.pg_class WHERE relname = 'users')
              AND att.attname = '{column}'
            LIMIT 1;

            IF cons_name IS NOT NULL THEN
                EXECUTE 'ALTER TABLE {table} DROP CONSTRAINT ' || cons_name;
            END IF;
        END $$;
        """
    )


def _drop_all_fks() -> None:
    for table, old_col, _new_col, _on_delete in FK_SPECS:
        _drop_fk_for_column(table, old_col)


def _rename_columns() -> None:
    for table, old_col, new_col, _on_delete in FK_SPECS:
        if old_col == new_col:
            continue
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_tables WHERE tablename = '{table}') THEN
                    EXECUTE 'ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}';
                END IF;
            END $$;
            """
        )


def _recreate_fks() -> None:
    for table, _old_col, new_col, on_delete in FK_SPECS:
        constraint_name = f"fk_{table}_{new_col}_accounts"
        fk_sql = (
            f"FOREIGN KEY ({new_col}) REFERENCES accounts(id)"
            if on_delete == "NO ACTION"
            else f"FOREIGN KEY ({new_col}) REFERENCES accounts(id) ON DELETE {on_delete}"
        )
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_tables WHERE tablename = '{table}') THEN
                    EXECUTE 'ALTER TABLE {table} ADD CONSTRAINT {constraint_name} {fk_sql}';
                END IF;
            END $$;
            """
        )


def upgrade() -> None:
    # 1. Create accounts table
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("auth_provider", sa.String(20), nullable=False, server_default="local"),
        sa.Column("sso_subject", sa.String(512), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("preferences", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("is_system_admin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_accounts_email"),
        sa.CheckConstraint("auth_provider IN ('local', 'oidc', 'saml', 'scim')", name="ck_accounts_auth_provider"),
    )

    # 2. Create org_memberships table
    op.create_table(
        "org_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="runner"),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("account_id", "organisation_id", name="uq_org_memberships_account_org"),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'operator', 'runner', 'viewer')",
            name="ck_org_memberships_role",
        ),
    )
    op.create_index(op.f("ix_org_memberships_account_id"), "org_memberships", ["account_id"], unique=False)
    op.create_index(op.f("ix_org_memberships_organisation_id"), "org_memberships", ["organisation_id"], unique=False)

    # 3. Migrate data from users -> accounts
    # Use DISTINCT ON email in case the same email exists in multiple orgs.
    op.execute(
        """
        INSERT INTO accounts (id, email, display_name, password_hash, auth_provider,
                              sso_subject, active, last_login, preferences, is_system_admin,
                              created_at, updated_at)
        SELECT DISTINCT ON (u.email)
               u.id, u.email, u.display_name, u.password_hash, u.auth_provider,
               u.sso_subject, u.active, u.last_login, u.preferences, false,
               u.created_at, u.updated_at
        FROM users u
        ORDER BY u.email, u.created_at
        ON CONFLICT (email) DO NOTHING
        """
    )

    # 4. Migrate data from users -> org_memberships
    op.execute(
        """
        INSERT INTO org_memberships (id, account_id, organisation_id, role, joined_at, created_at, updated_at)
        SELECT gen_random_uuid() AS id, a.id, u.organisation_id, u.org_role, u.created_at,
               u.created_at, u.updated_at
        FROM users u
        JOIN accounts a ON a.email = u.email
        """
    )

    # 5. Promote earliest-created admin in each org to 'owner'
    op.execute(
        """
        UPDATE org_memberships om
        SET role = 'owner'
        FROM (
            SELECT DISTINCT ON (organisation_id) id
            FROM org_memberships
            WHERE role = 'admin'
            ORDER BY organisation_id, joined_at
        ) AS first_admins
        WHERE om.id = first_admins.id
        """
    )

    # 6. Drop all FK constraints on users.id
    _drop_all_fks()

    # 7. Rename FK columns to account_id
    _rename_columns()

    # 8. Recreate FKs pointing to accounts.id
    _recreate_fks()

    # 9. Drop users table
    op.drop_table("users")


def downgrade() -> None:
    # This is a one-way migration. Reversal would recreate users and
    # reverse-map accounts + memberships back, which is lossy.
    raise NotImplementedError("Downgrade not supported for account model migration")
