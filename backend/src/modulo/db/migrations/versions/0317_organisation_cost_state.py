"""Organisation cost state table (FAR-1823 Land 2, additive half).

Creates ``organisation_cost_state`` and backfills one row per existing
``organisations`` row, copying the three cost columns (``max_run_cost_cents``,
``spend_ceiling_cents``, ``org_cumulative_spend_cents``).

This is the ADDITIVE half only: the three ``organisations`` columns of the same
names are left in place and no reader or writer is converted here. A follow-up
change moves the finalize gate+accrual and the ceiling set/clear endpoints onto
this row under a per-org advisory lock, then drops the old columns.

Tenancy: the table is keyed ``org_id`` (not ``organisation_id``), so it sits
outside the ``rls_org_isolation`` regime — that policy predicate and the ORM
tenant filter both key on the exact column name ``organisation_id``. This
mirrors the ``org_id`` naming rule ``system_audit_events`` documents; access is
always by an explicit organisation id. No ROW LEVEL SECURITY is enabled here.

Aditively reversible: the downgrade drops the table.

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


def downgrade() -> None:
    op.drop_table(_TABLE)
