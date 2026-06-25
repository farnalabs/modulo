"""Add OAuth 2.0 tables for MCP authorization code flow.

Revision ID: 0016_oauth_tables
Revises: 0015_org_deletion
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_oauth_tables"
down_revision: str | Sequence[str] | None = "0015_org_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("client_secret_hash", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, comment="Space-separated scope values"),
        sa.Column("redirect_uris", sa.Text(), nullable=False, comment="Space-separated allowed redirect URIs"),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", name="uq_oauth_clients_client_id"),
    )
    op.create_index(
        op.f("ix_oauth_clients_client_id"), "oauth_clients", ["client_id"], unique=True
    )
    op.create_index(
        op.f("ix_oauth_clients_organisation_id"), "oauth_clients", ["organisation_id"], unique=False
    )

    op.create_table(
        "oauth_authorization_codes",
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, comment="Space-separated requested scopes"),
        sa.Column("redirect_uri", sa.String(1024), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=True, comment="PKCE S256 challenge"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index(
        op.f("ix_oauth_authorization_codes_client_id"),
        "oauth_authorization_codes",
        ["client_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_authorization_codes_organisation_id"),
        "oauth_authorization_codes",
        ["organisation_id"],
        unique=False,
    )

    op.create_table(
        "oauth_token_families",
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("max_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_blacklisted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("blacklisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("family_id"),
    )
    op.create_index(
        op.f("ix_oauth_token_families_client_id"),
        "oauth_token_families",
        ["client_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_token_families_organisation_id"),
        "oauth_token_families",
        ["organisation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_token_families_organisation_id"), table_name="oauth_token_families")
    op.drop_index(op.f("ix_oauth_token_families_client_id"), table_name="oauth_token_families")
    op.drop_table("oauth_token_families")
    op.drop_index(
        op.f("ix_oauth_authorization_codes_organisation_id"),
        table_name="oauth_authorization_codes",
    )
    op.drop_index(
        op.f("ix_oauth_authorization_codes_client_id"),
        table_name="oauth_authorization_codes",
    )
    op.drop_table("oauth_authorization_codes")
    op.drop_index(op.f("ix_oauth_clients_organisation_id"), table_name="oauth_clients")
    op.drop_index(op.f("ix_oauth_clients_client_id"), table_name="oauth_clients")
    op.drop_table("oauth_clients")
