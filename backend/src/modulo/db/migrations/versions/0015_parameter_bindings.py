"""Add parameter_bindings_json to pipeline_snapshots.

Implements RFC §10 Phase 2: snapshot-time parameter resolution storage.

Changes:
  - Add parameter_bindings_json column to pipeline_snapshots table

Revision ID: 0015_parameter_bindings
Revises: 0014_parameter_schemas
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_parameter_bindings"
down_revision: str | None = "0014_parameter_schemas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_snapshots",
        sa.Column("parameter_bindings_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline_snapshots", "parameter_bindings_json")
