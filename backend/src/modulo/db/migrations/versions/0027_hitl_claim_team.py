"""Add required_team_id column to hitl_claims for team-scoped HITL gates.

Revision ID: 0027_hitl_claim_team
Revises: 0026_team_rbac_cap
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_hitl_claim_team"
down_revision: str | Sequence[str] | None = "0026_team_rbac_cap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hitl_claims",
        sa.Column(
            "required_team_id",
            sa.Uuid(),
            sa.ForeignKey("teams.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_hitl_claims_required_team_id_teams",
        "hitl_claims",
        type_="foreignkey",
    )
    op.drop_column("hitl_claims", "required_team_id")
