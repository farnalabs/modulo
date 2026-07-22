"""Create snapshot_schema_pins table

Revision ID: 0022_create_snapshot_schema_pins_table
Revises: 0021_add_system_bool_to_schemas
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_create_snapshot_schema_pins_table"
down_revision = "0021_add_system_bool_to_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "snapshot_schema_pins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.CheckConstraint("direction IN ('input', 'output')", name="ck_snapshot_schema_pins_direction"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["pipeline_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["schemas.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id", "schema_version"],
            ["schema_versions.schema_id", "schema_versions.version"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ssp_snapshot", "snapshot_schema_pins", ["snapshot_id"])
    op.create_index("idx_ssp_schema", "snapshot_schema_pins", ["schema_id", "schema_version"])


def downgrade() -> None:
    op.drop_index("idx_ssp_schema", table_name="snapshot_schema_pins")
    op.drop_index("idx_ssp_snapshot", table_name="snapshot_schema_pins")
    op.drop_table("snapshot_schema_pins")
