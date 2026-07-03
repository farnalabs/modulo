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
    # Add run_number to runs
    op.add_column("runs", sa.Column("run_number", sa.Integer(), nullable=True))
    
    # Backfill run_number sequentially per organisation_id
    conn = op.get_bind()
    conn.execute(text("""
        WITH numbered AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY organisation_id ORDER BY created_at) AS rn
            FROM runs
        )
        UPDATE runs SET run_number = numbered.rn
        FROM numbered
        WHERE runs.id = numbered.id
    """))
    
    # Make run_number non-nullable now
    op.alter_column("runs", "run_number", nullable=False)
    
    # Add session_number to chat_sessions
    op.add_column("chat_sessions", sa.Column("session_number", sa.Integer(), nullable=True))
    
    # Backfill session_number sequentially per user_id
    conn.execute(text("""
        WITH numbered AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at) AS rn
            FROM chat_sessions
        )
        UPDATE chat_sessions SET session_number = numbered.rn
        FROM numbered
        WHERE chat_sessions.id = numbered.id
    """))
    
    # Make session_number non-nullable now
    op.alter_column("chat_sessions", "session_number", nullable=False)


def downgrade() -> None:
    op.drop_column("runs", "run_number")
    op.drop_column("chat_sessions", "session_number")
