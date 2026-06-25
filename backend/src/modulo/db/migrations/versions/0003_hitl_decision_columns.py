"""Add decision columns to hitl_claims.

Revision ID: 0003_hitl_decision_columns
Revises: 0002_rls_policies
Create Date: 2026-06-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_hitl_decision_columns"
down_revision: str | None = "0002_rls_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hitl_claims",
        sa.Column("decision", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "hitl_claims",
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_hitl_claims_decision",
        "hitl_claims",
        "decision IN ('approved', 'rejected')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_hitl_claims_decision", "hitl_claims", type_="check")
    op.drop_column("hitl_claims", "decision_at")
    op.drop_column("hitl_claims", "decision")
