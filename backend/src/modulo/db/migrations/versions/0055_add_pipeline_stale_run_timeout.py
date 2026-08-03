"""Add stale_run_timeout_minutes to pipelines.

Revision ID: 0025_add_pipeline_stale_run_timeout
Revises: 0024_add_saved_views_deleted_at
Create Date: 2026-07-26
"""

from alembic import op

revision = "0025_add_pipeline_stale_run_timeout"
down_revision = "0024_add_saved_views_deleted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS stale_run_timeout_minutes INTEGER")


def downgrade() -> None:
    op.drop_column("pipelines", "stale_run_timeout_minutes")
