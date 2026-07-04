"""Add tier column to connector_instances, model_backends, and library_primitives.

Revision ID: 0062_add_integration_tier
Revises: 0061_remy_context_sources
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062_add_integration_tier"
down_revision: str | Sequence[str] | None = "0061_remy_context_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("connector_instances", "model_backends", "library_primitives")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("tier", sa.String(20), nullable=False, server_default="native"),
        )
        op.create_check_constraint(
            f"ck_{table}_tier",
            table,
            sa.text("tier IN ('native', 'preview', 'in_dev')"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_tier", table, type_="check")
        op.drop_column(table, "tier")
