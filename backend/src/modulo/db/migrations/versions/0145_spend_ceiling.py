"""Add hard spend-ceiling columns to organisations (FAR-391, spec §5.1).

Revision ID: 0145_spend_ceiling
Revises: 0144_broaden_notification_status_in_app
Create Date: 2026-08-23

Adds three integer-cents columns backing the per-run / per-org hard spend
ceilings:

- ``max_run_cost_cents`` — per-run hard ceiling (NULL = unlimited).
- ``spend_ceiling_cents`` — org lifetime budget (NULL = unlimited).
- ``org_cumulative_spend_cents`` — running consumed total, incremented at each
  run's terminal ledger write (defaults to 0 so existing orgs start with no
  consumed budget and are never falsely over-ceiling).

Storing cents (not a ``Numeric`` USD) keeps the gate comparison exact and
allocation-free. No CHECK constraints: a NULL ceiling is "unlimited" and 0 is a
valid kill-switch, so a ``>= 0`` column check is sufficient and is enforced by
the application layer.

Postgres-only concern: ``server_default=0`` is portable (integer literal).
"""

from alembic import op
from sqlalchemy import Column, Integer

revision = "0145_spend_ceiling"
down_revision = "0144_broaden_notification_status_in_app"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organisations", Column("max_run_cost_cents", Integer(), nullable=True))
    op.add_column("organisations", Column("spend_ceiling_cents", Integer(), nullable=True))
    op.add_column(
        "organisations",
        Column("org_cumulative_spend_cents", Integer(), nullable=False, server_default="0"),
    )
    # Backfill existing rows so the NOT-NULL column is satisfied (they start at 0).
    op.execute("UPDATE organisations SET org_cumulative_spend_cents = 0 WHERE org_cumulative_spend_cents IS NULL")


def downgrade() -> None:
    op.drop_column("organisations", "org_cumulative_spend_cents")
    op.drop_column("organisations", "spend_ceiling_cents")
    op.drop_column("organisations", "max_run_cost_cents")
