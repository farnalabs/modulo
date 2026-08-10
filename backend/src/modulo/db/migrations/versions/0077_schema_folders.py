"""Add schema_folders table and folder_id to schemas.

Revision ID: 0077_schema_folders
Revises: 0076_analytics_concurrency_columns
Create Date: 2026-08-10

Schema folders mirror the pipeline_folders feature: a schema can be
assigned to an org-scoped folder via ``schemas.folder_id`` (nullable FK
with ON DELETE SET NULL), and folders can nest via ``parent_id`` with a
``sort_order`` used for manual reordering.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_schema_folders"
down_revision: str | None = "0076_analytics_concurrency_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRICT_RLS: tuple[str, ...] = ("schema_folders",)


def upgrade() -> None:
    op.create_table(
        "schema_folders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["schema_folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schema_folders_parent_id", "schema_folders", ["parent_id"], if_not_exists=True)
    op.create_index("ix_schema_folders_organisation_id", "schema_folders", ["organisation_id"], if_not_exists=True)

    op.execute("ALTER TABLE schemas ADD COLUMN IF NOT EXISTS folder_id UUID")
    op.execute(
        "ALTER TABLE schemas ADD CONSTRAINT fk_schemas_folder_id "
        "FOREIGN KEY (folder_id) REFERENCES schema_folders(id) ON DELETE SET NULL"
    )
    op.create_index("ix_schemas_folder_id", "schemas", ["folder_id"], if_not_exists=True)

    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    for table in _STRICT_RLS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({strict})'))


def downgrade() -> None:
    for table in _STRICT_RLS:
        op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{table}"'))
        op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    op.drop_index("ix_schemas_folder_id", table_name="schemas", if_exists=True)
    op.execute("ALTER TABLE schemas DROP CONSTRAINT IF EXISTS fk_schemas_folder_id")
    op.drop_column("schemas", "folder_id")
    op.drop_index("ix_schema_folders_organisation_id", table_name="schema_folders", if_exists=True)
    op.drop_index("ix_schema_folders_parent_id", table_name="schema_folders", if_exists=True)
    op.drop_table("schema_folders")
