"""Add archived_at to pipelines (post-squash fix).

Production pipelines table is missing archived_at because the migration
squash (89->4) stamped the DB to 0005 without applying the new DDL, which
folded archived_at into the CREATE TABLE in 0003 instead of an ALTER ADD.

Revision ID: 0006_post_squash_pipeline_archived_at
Revises: 0005_v2_features_system
Create Date: 2026-07-12

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_post_squash_pipeline_archived_at"
down_revision: str | None = "0005_v2_features_system"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    op.drop_column("pipelines", "archived_at")
