"""Add workspace-input drift column and audit partial index (FAR-801).

Revision ID: 0238_workspace_input_drift_and_audit
Revises: 0237_fix_token_family_org_nullable
Create Date: 2026-09-13

Chains on top of 0237_fix_token_family_org_nullable (head of main at the time
of the merge-conflict fix; FAR-801's migration was originally numbered 0233 but
that prefix collided with main's 0233_add_updated_at_audit_to_organisations, and
main had since advanced to 0237). In turn this chains on FAR-826's
0232_seed_modulo_sentinel_organisation via 0233_add_updated_at_audit_to_organisations
.. 0237_fix_token_family_org_nullable; this migration therefore does NOT re-add
the ``run_daily_facts.workspace_inputs_count`` column (it would collide on upgrade).

Schema legs (Postgres only; SQLite/ORM-created test schemas get the columns
from the ``Run`` / ``RunDailyFact`` models' ``create_all``):

1. ``runs.workspace_inputs_drift_detected`` — nullable ``boolean``, no
   default.  NULL = unknown / most-recently-no-inputs.  Written by the
   audit layer (``record_drift``) in the SAME transaction as the audit
   row so terminalization reads the correct value before classifying
   the run.

2. Partial index ``ix_run_node_outputs_audit`` on ``run_node_outputs
   (run_id, node_id) WHERE node_id = '__mwi_audit__'`` — the
   compensating sweep and the analytics backfill both query audit rows
   by ``(run_id, node_id)``; the partial index bounds the scan to audit
   rows only (a tiny fraction of the table).
"""

import sqlalchemy as sa
from alembic import op

revision = "0238_workspace_input_drift_and_audit"
down_revision = "0237_fix_token_family_org_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. runs.workspace_inputs_drift_detected
    op.add_column("runs", sa.Column("workspace_inputs_drift_detected", sa.Boolean(), nullable=True))

    # 2. Partial index for audit rows
    op.create_index(
        "ix_run_node_outputs_audit",
        "run_node_outputs",
        ["run_id", "node_id"],
        postgresql_where=sa.text("node_id = '__mwi_audit__'"),
    )


def downgrade() -> None:
    op.drop_index("ix_run_node_outputs_audit", table_name="run_node_outputs")
    op.drop_column("runs", "workspace_inputs_drift_detected")
