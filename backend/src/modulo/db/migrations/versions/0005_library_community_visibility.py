"""Add community visibility to library_primitives.

Revision ID: 0005_library_community_visibility
Revises: 0004_pipeline_graph_nodes
Create Date: 2026-06-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_library_community_visibility"
down_revision: str | None = "0004_pipeline_graph_nodes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_library_primitives_visibility",
        "library_primitives",
        type_="check",
    )
    op.create_check_constraint(
        "ck_library_primitives_visibility",
        "library_primitives",
        "visibility IN ('org', 'team', 'community')",
    )

    op.drop_constraint(
        "ck_library_primitives_team_owner",
        "library_primitives",
        type_="check",
    )
    op.create_check_constraint(
        "ck_library_primitives_team_owner",
        "library_primitives",
        "visibility IN ('org', 'community') OR owner_team_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_library_primitives_visibility",
        "library_primitives",
        type_="check",
    )
    op.create_check_constraint(
        "ck_library_primitives_visibility",
        "library_primitives",
        "visibility IN ('org', 'team')",
    )

    op.drop_constraint(
        "ck_library_primitives_team_owner",
        "library_primitives",
        type_="check",
    )
    op.create_check_constraint(
        "ck_library_primitives_team_owner",
        "library_primitives",
        "visibility = 'org' OR owner_team_id IS NOT NULL",
    )
