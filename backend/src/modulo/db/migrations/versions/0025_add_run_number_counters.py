"""Add per-organisation counter table for run numbers.

Replaces the expensive SELECT MAX(run_number) ... WHERE organisation_id = :oid
pattern with an atomic INSERT ... ON CONFLICT DO UPDATE ... RETURNING counter.

Revision ID: 0025_add_run_number_counters
Revises: 0024_backfill_schema_pins
Create Date: 2026-07-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_add_run_number_counters"
down_revision = "0024_backfill_schema_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_number_counters",
        sa.Column("organisation_id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("next_run_number", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_table("run_number_counters")
