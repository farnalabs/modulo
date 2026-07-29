"""Add claimed_by column to runs table for worker identity tracking."""

import sqlalchemy as sa
from alembic import op

revision = "0027_add_claimed_by_to_runs"
down_revision = "0026_add_agent_commands"


def upgrade():
    op.add_column("runs", sa.Column("claimed_by", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("runs", "claimed_by")
