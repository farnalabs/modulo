"""Add template_id and agent_command to agents

Revision ID: 0017_agent_template_fields
Revises: 0016_composite_parameter_schema
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_agent_template_fields"
down_revision = "0016_composite_parameter_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("template_id", sa.String(255), nullable=True))
    op.add_column("agents", sa.Column("agent_command", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "agent_command")
    op.drop_column("agents", "template_id")
