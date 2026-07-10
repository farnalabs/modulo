"""Add lifecycle_maps table and lifecycle_map library primitive type.

Revision ID: 0086_lifecycle_maps
Revises: 0085_mcp_setup_tokens
Create Date: 2026-07-10 19:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086_lifecycle_maps"
down_revision: str | Sequence[str] | None = "0085_mcp_setup_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lifecycle_maps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "organisation_id",
            sa.Uuid(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("owner_team_id", sa.Uuid(), sa.ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("visibility", sa.String(10), nullable=False, server_default="org"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_check_constraint(
        "ck_lifecycle_maps_visibility",
        "lifecycle_maps",
        sa.text("visibility IN ('org', 'team')"),
    )
    op.create_check_constraint(
        "ck_lifecycle_maps_team_owner",
        "lifecycle_maps",
        sa.text("visibility = 'org' OR owner_team_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_lifecycle_maps_version",
        "lifecycle_maps",
        sa.text("version > 0"),
    )

    op.drop_constraint("ck_library_primitives_type", "library_primitives")
    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        sa.text(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', "
            "'test_fixture', 'pipeline_template', 'composite', 'lifecycle_map')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint("ck_library_primitives_type", "library_primitives")
    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        sa.text(
            "primitive_type IN ('schema', 'workflow', 'agent', 'integration', "
            "'test_fixture', 'pipeline_template', 'composite')"
        ),
    )
    op.drop_table("lifecycle_maps")
