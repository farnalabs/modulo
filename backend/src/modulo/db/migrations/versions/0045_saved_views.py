"""Create saved_views table for persisted filter/layout configurations.

Revision ID: 0045_saved_views
Revises: 0044_library_auto_update
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045_saved_views"
down_revision: str | Sequence[str] | None = "0044_library_auto_update"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS saved_views (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            view_type VARCHAR(50) NOT NULL,
            filters JSONB NOT NULL DEFAULT '{}',
            columns JSONB,
            sort_by VARCHAR(100),
            sort_order VARCHAR(10) NOT NULL DEFAULT 'desc',
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_saved_views_type CHECK (view_type IN ('run_list', 'pipeline_list', 'audit_log'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_saved_views_organisation_id ON saved_views(organisation_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS saved_views")
