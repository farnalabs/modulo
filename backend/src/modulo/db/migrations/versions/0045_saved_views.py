"""Create saved_views table.

Revision ID: 0045_saved_views
Revises: 0044_library_auto_update
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_saved_views"
down_revision: str | None = "0044_library_auto_update"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_views",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("view_type", sa.String(50), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("columns", sa.JSON(), nullable=True),
        sa.Column("sort_by", sa.String(100), nullable=True),
        sa.Column("sort_order", sa.String(10), nullable=False, server_default="desc"),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("view_type IN ('run_list', 'pipeline_list', 'audit_log')", name="ck_saved_views_type"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_saved_views_organisation_id"), "saved_views", ["organisation_id"])

    op.execute("ALTER TABLE saved_views ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY rls_org_isolation ON saved_views "
        "USING (organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON saved_views")
    op.execute("ALTER TABLE saved_views DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_saved_views_organisation_id"), table_name="saved_views")
    op.drop_table("saved_views")
