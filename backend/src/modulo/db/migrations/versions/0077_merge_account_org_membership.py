"""Merge account_org_membership branch back into main chain.

Revision ID: 0077_merge_account_org_membership
Revises: 0076_merge_node_observations, 0074_account_org_membership
Create Date: 2026-07-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_merge_account_org_membership"
down_revision: str | Sequence[str] | None = (
    "0076_merge_node_observations",
    "0074_account_org_membership",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
