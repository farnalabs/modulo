"""Add workspace-input drift column, daily-facts count, and audit partial index (FAR-801).

Revision ID: 0223_workspace_input_drift_and_audit
Revises: 0222_journey_provenance
Create Date: 2026-09-13

Schema legs (Postgres only; SQLite/ORM-created test schemas get the columns
from the ``Run`` / ``RunDailyFact`` models' ``create_all``):

1. ``runs.workspace_inputs_drift_detected`` — nullable ``boolean``, no
   default.  NULL = unknown / most-recently-no-inputs.  Written by the
   audit layer (``record_drift``) in the SAME transaction as the audit
   row so terminalization reads the correct value before classifying
   the run.

2. ``run_daily_facts.workspace_inputs_count`` — nullable ``integer``, no
   default.  The count of managed workspace inputs resolved for the run,
   computed by the analytics backfill from the audit row in
   ``run_node_outputs``.

3. Partial index ``ix_run_node_outputs_audit`` on ``run_node_outputs
   (run_id, node_id) WHERE node_id = '__mwi_audit__'`` — the
   compensating sweep and the analytics backfill both query audit rows
   by ``(run_id, node_id)``; the partial index bounds the scan to audit
   rows only (a tiny fraction of the table).
"""

import sqlalchemy as sa
from alembic import op

revision = "0223_workspace_input_drift_and_audit"
down_revision = "0222_journey_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. runs.workspace_inputs_drift_detected
    op.add_column("runs", sa.Column("workspace_inputs_drift_detected", sa.Boolean(), nullable=True))

    # 2. run_daily_facts.workspace_inputs_count
    op.add_column("run_daily_facts", sa.Column("workspace_inputs_count", sa.Integer(), nullable=True))

    # 3. Partial index for audit rows
    op.create_index(
        "ix_run_node_outputs_audit",
        "run_node_outputs",
        ["run_id", "node_id"],
        postgresql_where=sa.text("node_id = '__mwi_audit__'"),
    )


def downgrade() -> None:
    op.drop_index("ix_run_node_outputs_audit", table_name="run_node_outputs")
    op.drop_column("run_daily_facts", "workspace_inputs_count")
    op.drop_column("runs", "workspace_inputs_drift_detected")
