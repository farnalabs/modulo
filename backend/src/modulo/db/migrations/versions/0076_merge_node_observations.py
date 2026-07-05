"""Merge node_observations branch back into main chain.

Revision ID: 0076_merge_node_observations
Revises: 0065_merge_add_schema_deprecation, 0073_node_observations
Create Date: 2026-07-04
"""
from collections.abc import Sequence

revision: str = "0076_merge_node_observations"
down_revision: str | Sequence[str] | None = (
    "0065_merge_add_schema_deprecation",
    "0073_node_observations",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
