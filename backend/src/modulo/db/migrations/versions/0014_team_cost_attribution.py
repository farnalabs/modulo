"""Team cost attribution.

Adds:
- owner_team_id FK on runs
- daily_spend_limit on organisations and teams
- team_id column on org_daily_run_counts with updated unique constraint

Revision ID: 0014_team_cost_attribution
Revises: 0013_environment_profiles_workspace_leases
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_team_cost_attribution"
down_revision: str | None = "0013_environment_profiles_workspace_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Run: owner_team_id ---
    op.add_column(
        "runs",
        sa.Column("owner_team_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        op.f("ix_runs_owner_team_id"),
        "runs",
        ["owner_team_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_runs_owner_team",
        "runs",
        "teams",
        ["owner_team_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- Organisation: daily_spend_limit ---
    op.add_column(
        "organisations",
        sa.Column("daily_spend_limit", sa.Numeric(14, 6), nullable=True),
    )

    # --- Team: daily_spend_limit ---
    op.add_column(
        "teams",
        sa.Column("daily_spend_limit", sa.Numeric(14, 6), nullable=True),
    )

    # --- OrgDailyRunCount: team_id ---
    # Drop the old unique constraint before adding the new column + constraint
    op.drop_constraint(
        "uq_org_daily_run_counts_date",
        "org_daily_run_counts",
        type_="unique",
    )
    op.add_column(
        "org_daily_run_counts",
        sa.Column("team_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        op.f("ix_org_daily_run_counts_team_id"),
        "org_daily_run_counts",
        ["team_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_org_daily_run_counts_team",
        "org_daily_run_counts",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_org_daily_run_counts_org_team_date",
        "org_daily_run_counts",
        ["organisation_id", "team_id", "run_date"],
    )


def downgrade() -> None:
    # --- OrgDailyRunCount: team_id ---
    op.drop_constraint(
        "uq_org_daily_run_counts_org_team_date",
        "org_daily_run_counts",
        type_="unique",
    )
    op.drop_constraint(
        "fk_org_daily_run_counts_team",
        "org_daily_run_counts",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_org_daily_run_counts_team_id"),
        table_name="org_daily_run_counts",
    )
    op.drop_column("org_daily_run_counts", "team_id")
    op.create_unique_constraint(
        "uq_org_daily_run_counts_date",
        "org_daily_run_counts",
        ["organisation_id", "run_date"],
    )

    # --- Team: daily_spend_limit ---
    op.drop_column("teams", "daily_spend_limit")

    # --- Organisation: daily_spend_limit ---
    op.drop_column("organisations", "daily_spend_limit")

    # --- Run: owner_team_id ---
    op.drop_constraint("fk_runs_owner_team", "runs", type_="foreignkey")
    op.drop_index(op.f("ix_runs_owner_team_id"), table_name="runs")
    op.drop_column("runs", "owner_team_id")
