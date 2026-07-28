"""add agent_commands column"""

from alembic import op
import sqlalchemy as sa


revision = "0026_add_agent_commands"
down_revision = ("0025_add_run_number_counters", "0025_add_pipeline_stale_run_timeout")


def upgrade():
    op.add_column("agents", sa.Column("agent_commands", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("agents", "agent_commands")
