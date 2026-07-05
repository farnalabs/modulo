"""Merge error_tracking branch back into main chain.

Revision ID: 0078_merge_error_tracking
Revises: 0077_merge_account_org_membership, 0075_error_tracking
Create Date: 2026-07-04
"""
from collections.abc import Sequence

revision: str = "0078_merge_error_tracking"
down_revision: str | Sequence[str] | None = (
    "0077_merge_account_org_membership",
    "0075_error_tracking",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
