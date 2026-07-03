"""Add run_number and session_number for human-friendly IDs

Revision ID: 0058_uuid_friendly_ids
Revises: 0057_notifications
Create Date: 2026-07-03 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0058_uuid_friendly_ids"
down_revision = "0057_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add run_number to runs (nullable initially)
    op.add_column("runs", sa.Column("run_number", sa.Integer(), nullable=True))

    # Backfill run_number sequentially per organisation_id
    conn = op.get_bind()
    conn.execute(text("""
        UPDATE runs SET run_number = (
            SELECT rn FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY organisation_id ORDER BY created_at) AS rn
                FROM runs
            ) numbered
            WHERE numbered.id = runs.id
        )
    """))
    
    # Make run_number non-nullable now
    op.alter_column("runs", "run_number", nullable=False)
    
    # Add unique constraint on (organisation_id, run_number) to prevent race-condition duplicates
    op.create_unique_constraint("uq_runs_org_run_number", "runs", ["organisation_id", "run_number"])
    
    # Add session_number to chat_sessions (nullable initially)
    op.add_column("chat_sessions", sa.Column("session_number", sa.Integer(), nullable=True))

    # Backfill session_number sequentially per user_id
    conn.execute(text("""
        UPDATE chat_sessions SET session_number = (
            SELECT rn FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at) AS rn
                FROM chat_sessions
            ) numbered
            WHERE numbered.id = chat_sessions.id
        )
    """))
    
    # Make session_number non-nullable now
    op.alter_column("chat_sessions", "session_number", nullable=False)
    
    # Add unique constraint on (user_id, session_number)
    op.create_unique_constraint("uq_chat_sessions_user_session_number", "chat_sessions", ["user_id", "session_number"])


def downgrade() -> None:
    op.drop_constraint("uq_runs_org_run_number", "runs", type_="unique")
    op.drop_column("runs", "run_number")
    op.drop_constraint("uq_chat_sessions_user_session_number", "chat_sessions", type_="unique")
    op.drop_column("chat_sessions", "session_number")
