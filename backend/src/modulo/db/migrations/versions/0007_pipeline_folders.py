"""Add pipeline_folders table and folder_id to pipelines.

Revision ID: 0007_pipeline_folders
Revises: 0006_post_squash_pipeline_archived_at
Create Date: 2026-07-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_pipeline_folders"
down_revision: str | None = "0006_post_squash_pipeline_archived_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_folders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["pipeline_folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pipeline_folders_parent_id", "pipeline_folders", ["parent_id"], if_not_exists=True)
    op.create_index("ix_pipeline_folders_organisation_id", "pipeline_folders", ["organisation_id"], if_not_exists=True)

    op.execute("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS folder_id UUID")
    op.execute(
        "ALTER TABLE pipelines ADD CONSTRAINT fk_pipelines_folder_id "
        "FOREIGN KEY (folder_id) REFERENCES pipeline_folders(id) ON DELETE SET NULL"
    )
    op.create_index("ix_pipelines_folder_id", "pipelines", ["folder_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_pipelines_folder_id", table_name="pipelines", if_exists=True)
    op.execute("ALTER TABLE pipelines DROP CONSTRAINT IF EXISTS fk_pipelines_folder_id")
    op.drop_column("pipelines", "folder_id")
    op.drop_index("ix_pipeline_folders_organisation_id", table_name="pipeline_folders", if_exists=True)
    op.drop_index("ix_pipeline_folders_parent_id", table_name="pipeline_folders", if_exists=True)
    op.drop_table("pipeline_folders")
