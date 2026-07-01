"""Create nodes and node_categories tables if they don't exist.

The Node and NodeCategory ORM models exist in the codebase, but no migration
was ever written to CREATE their backing tables.  Migration 0045 tries to
rename columns on them, so without this fix fresh installations would fail.

This migration runs after all prior migrations (including 0045) and uses
CREATE TABLE IF NOT EXISTS so it's a no-op on deployments where the tables
were created outside the migration chain.

Revision ID: 0047_create_missing_tables
Revises: 0046_system_config
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047_create_missing_tables"
down_revision: str | Sequence[str] | None = "0046_system_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = "0045_account_org_membership"


def upgrade() -> None:
    _create_nodes_if_not_exists()
    _create_node_categories_if_not_exists()


def _create_nodes_if_not_exists() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
            pipeline_id     UUID NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
            name            VARCHAR(255) NOT NULL,
            description     TEXT,
            parent_node_id  UUID REFERENCES nodes(id) ON DELETE SET NULL,
            timeout_seconds INTEGER,
            retry_count     INTEGER,
            retry_delay_seconds INTEGER,
            account_id      UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_nodes_timeout_seconds CHECK (timeout_seconds IS NULL OR timeout_seconds > 0),
            CONSTRAINT ck_nodes_retry_count CHECK (retry_count IS NULL OR retry_count >= 0),
            CONSTRAINT ck_nodes_retry_delay_seconds CHECK (retry_delay_seconds IS NULL OR retry_delay_seconds >= 0)
        )
        """
    )
    op.create_index(op.f("ix_nodes_organisation_id"), "nodes", ["organisation_id"], unique=False, if_not_exists=True)
    op.create_index(op.f("ix_nodes_pipeline_id"), "nodes", ["pipeline_id"], unique=False, if_not_exists=True)
    op.create_index(op.f("ix_nodes_parent_node_id"), "nodes", ["parent_node_id"], unique=False, if_not_exists=True)


def _create_node_categories_if_not_exists() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS node_categories (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
            name            VARCHAR(100) NOT NULL,
            description     TEXT,
            color           VARCHAR(7) NOT NULL DEFAULT '#6366f1',
            icon            VARCHAR(50),
            sort_order      INTEGER NOT NULL DEFAULT 0,
            account_id      UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_node_categories_org_name UNIQUE (organisation_id, name)
        )
        """
    )
    op.create_index(
        op.f("ix_node_categories_organisation_id"),
        "node_categories",
        ["organisation_id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("node_categories")
    op.drop_table("nodes")
