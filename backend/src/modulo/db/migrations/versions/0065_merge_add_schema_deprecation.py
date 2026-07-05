"""Merge add_schema_deprecation branch back into main chain.

Revision ID: 0065_merge_add_schema_deprecation
Revises: 0063_library_community_source, 0064_add_schema_deprecation
Create Date: 2026-07-04
"""
from collections.abc import Sequence

revision: str = "0065_merge_add_schema_deprecation"
down_revision: str | Sequence[str] | None = (
    "0063_library_community_source",
    "0064_add_schema_deprecation",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
