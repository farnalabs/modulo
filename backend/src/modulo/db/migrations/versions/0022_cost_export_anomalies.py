"""Create scheduled_reports and spend_anomalies tables.

Revision ID: 0022_cost_export_anomalies
Revises: 0021_sso_providers
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_cost_export_anomalies"
down_revision: str | Sequence[str] | None = "0021_sso_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.String(20), nullable=False),
        sa.Column("group_by", sa.String(20), nullable=False),
        sa.Column("format", sa.String(10), nullable=False, server_default="csv"),
        sa.Column("recipients", sa.JSON(), nullable=False, default=list),
        sa.Column("schedule_type", sa.String(20), nullable=False, server_default="one_time"),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"], ["organisations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_reports_organisation_id",
        "scheduled_reports",
        ["organisation_id"],
    )

    op.create_table(
        "spend_anomalies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("anomaly_date", sa.Date(), nullable=False),
        sa.Column("pipeline_id", sa.UUID(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 6), nullable=False),
        sa.Column("baseline", sa.Numeric(14, 6), nullable=False),
        sa.Column("percent_above", sa.Numeric(8, 2), nullable=False),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"], ["organisations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_id"], ["pipelines.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_spend_anomalies_anomaly_date",
        "spend_anomalies",
        ["anomaly_date"],
    )
    op.create_index(
        "ix_spend_anomalies_organisation_id",
        "spend_anomalies",
        ["organisation_id"],
    )


def downgrade() -> None:
    op.drop_table("spend_anomalies")
    op.drop_table("scheduled_reports")
