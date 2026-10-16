"""Organisation cost state table + cutover (FAR-1823 Land 2).

Creates ``organisation_cost_state``, backfills one row per existing
``organisations`` row (copying ``max_run_cost_cents`` / ``spend_ceiling_cents`` /
``org_cumulative_spend_cents``), then DROPS those three ``organisations`` columns
and the ``ck_organisations_cum_spend`` CHECK that referenced the cumulative one.

Tenancy: the table is keyed ``org_id`` (not ``organisation_id``), so it sits
outside the ``rls_org_isolation`` regime — that policy predicate and the ORM
tenant filter both key on the exact column name ``organisation_id``. This
mirrors the ``org_id`` naming rule ``system_audit_events`` documents; access is
always by an explicit organisation id. No ROW LEVEL SECURITY is enabled here.

Additively reversible: the downgrade re-adds the three ``organisations`` columns,
backfills them from the cost-state row, re-adds the CHECK, then drops the table —
so a rollback does not lose data.

Revision ID: 0317_organisation_cost_state
Revises: 0316_policy_gate_evaluation_mode
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0317_organisation_cost_state"
down_revision: str | None = "0316_policy_gate_evaluation_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organisation_cost_state"
_CUM_SPEND_CHECK = "ck_organisation_cost_state_cum_spend"
_ORG_CUM_SPEND_CHECK = "ck_organisations_cum_spend"

# Backfill: one row per existing organisation, copying the three current values.
# The table is created empty by this same upgrade, so no org can already have a
# row and the plain INSERT cannot conflict. Static literal (no f-string / no
# interpolation) — the migration-fstring-sql rule for revisions above 0247.
_BACKFILL_SQL = (
    "INSERT INTO organisation_cost_state "
    "(org_id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents) "
    "SELECT id, max_run_cost_cents, spend_ceiling_cents, org_cumulative_spend_cents "
    "FROM organisations"
)

# Reverse backfill (downgrade): copy the values back onto the re-added
# ``organisations`` columns. An org with no cost-state row keeps the column
# defaults (ceiling NULL, cumulative 0). Static literal.
_RESTORE_ORG_COLUMNS_SQL = (
    "UPDATE organisations o "
    "SET max_run_cost_cents = s.max_run_cost_cents, "
    "    spend_ceiling_cents = s.spend_ceiling_cents, "
    "    org_cumulative_spend_cents = s.org_cumulative_spend_cents "
    "FROM organisation_cost_state s "
    "WHERE s.org_id = o.id"
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("max_run_cost_cents", sa.Integer(), nullable=True),
        sa.Column("spend_ceiling_cents", sa.Integer(), nullable=True),
        sa.Column("org_cumulative_spend_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["org_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
        sa.CheckConstraint("org_cumulative_spend_cents >= 0", name=_CUM_SPEND_CHECK),
    )
    op.execute(_BACKFILL_SQL)
    # Cutover: drop the CHECK that references the cumulative column first, then
    # the three columns themselves.
    op.drop_constraint(_ORG_CUM_SPEND_CHECK, "organisations", type_="check")
    op.drop_column("organisations", "max_run_cost_cents")
    op.drop_column("organisations", "spend_ceiling_cents")
    op.drop_column("organisations", "org_cumulative_spend_cents")


def downgrade() -> None:
    # Genuine inverse: re-add the three columns (shape from 0145), backfill them
    # from the cost-state row, re-add the CHECK, then drop the new table.
    op.add_column("organisations", sa.Column("max_run_cost_cents", sa.Integer(), nullable=True))
    op.add_column("organisations", sa.Column("spend_ceiling_cents", sa.Integer(), nullable=True))
    op.add_column(
        "organisations",
        sa.Column("org_cumulative_spend_cents", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.execute(_RESTORE_ORG_COLUMNS_SQL)
    op.create_check_constraint(_ORG_CUM_SPEND_CHECK, "organisations", "org_cumulative_spend_cents >= 0")
    op.drop_table(_TABLE)
