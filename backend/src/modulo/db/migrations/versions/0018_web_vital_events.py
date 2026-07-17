"""Add web_vital_events table

Revision ID: 0018_web_vital_events
Revises: 0017_agent_template_fields
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_web_vital_events"
down_revision = "0017_agent_template_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_vital_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("metric_name", sa.String(50), nullable=False, index=True),
        sa.Column("metric_value", sa.Float(), nullable=False),
        sa.Column("metric_rating", sa.String(20), nullable=True),
        sa.Column("route_path", sa.String(500), nullable=True),
        sa.Column("page_url", sa.String(2000), nullable=True),
        sa.Column("navigation_type", sa.String(50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_web_vital_events_metric_name_recorded", "web_vital_events", ["metric_name", "recorded_at"])
    op.execute("ALTER TABLE web_vital_events ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY rls_org_isolation ON web_vital_events
        USING (organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid)
    """)


def downgrade() -> None:
    op.drop_table("web_vital_events")
