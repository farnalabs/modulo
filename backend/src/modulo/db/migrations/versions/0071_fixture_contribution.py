"""Add test_fixture type, contribution_status, run IO columns.

Adds:
- 'test_fixture' to library_primitives primitive_type check constraint
- contribution_status column on library_primitives
- input_payload and outputs_json columns on runs

Revision ID: 0071_fixture_contribution
Revises: 0013_environment_profiles_workspace_leases
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071_fixture_contribution"
down_revision: str | None = "0013_environment_profiles_workspace_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_library_primitives_type", "library_primitives", type_="check")

    op.add_column(
        "library_primitives",
        sa.Column("contribution_status", sa.String(20), nullable=True),
    )

    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        "primitive_type IN ('schema', 'workflow', 'agent', 'integration', 'test_fixture')",
    )

    op.create_check_constraint(
        "ck_library_primitives_contribution_status",
        "library_primitives",
        "contribution_status IN ('draft', 'review_queue', 'published')",
    )

    op.add_column(
        "runs",
        sa.Column("input_payload", sa.JSON(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("outputs_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "outputs_json")
    op.drop_column("runs", "input_payload")
    op.drop_column("library_primitives", "contribution_status")
    op.drop_constraint(
        "ck_library_primitives_contribution_status",
        "library_primitives",
        type_="check",
    )
    op.drop_constraint("ck_library_primitives_type", "library_primitives", type_="check")
    op.create_check_constraint(
        "ck_library_primitives_type",
        "library_primitives",
        "primitive_type IN ('schema', 'workflow', 'agent', 'integration')",
    )
