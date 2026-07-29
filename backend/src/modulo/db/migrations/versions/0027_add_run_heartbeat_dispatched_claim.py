"""add heartbeat_at, dispatched_at, claim_count to runs

Revision ID: 0027_add_run_heartbeat_dispatched_claim
Revises: 0026_add_agent_commands
Create Date: 2026-07-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_add_run_heartbeat_dispatched_claim"
down_revision = "0026_add_agent_commands"


def upgrade():
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("runs", "claim_count")
    op.drop_column("runs", "dispatched_at")
    op.drop_column("runs", "heartbeat_at")
