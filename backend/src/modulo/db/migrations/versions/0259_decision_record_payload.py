"""Add payload columns to policy_gate_decisions (FAR-1102 chunk 4).

Six descriptive payload columns added to the identity/constraint surface
created by chunk 1 (0250_eval_policy_gate).  The CHECK constraint on
``resolved_action`` is Postgres-only (dialect-guarded).

Revision ID: 0259_decision_record_payload
Revises: 0258_pipeline_accountability_owners
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0259_decision_record_payload"
down_revision = "0258_pipeline_accountability_owners"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # 1. Add the six payload columns.
    op.add_column(
        "policy_gate_decisions",
        sa.Column(
            "resolved_action",
            sa.Text(),
            server_default="'continue'",
            nullable=False,
        ),
    )
    op.add_column(
        "policy_gate_decisions",
        sa.Column("error_detail", sa.String(2000), nullable=True),
    )
    op.add_column(
        "policy_gate_decisions",
        sa.Column("node_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "policy_gate_decisions",
        sa.Column("eval_result_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "policy_gate_decisions",
        sa.Column("run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "policy_gate_decisions",
        sa.Column(
            "policy_gate_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )

    # 2. CHECK constraint on resolved_action — Postgres-only (dialect guard).
    if _is_postgres():
        op.create_check_constraint(
            "ck_policy_gate_decisions_resolved_action",
            "policy_gate_decisions",
            "resolved_action IN ('continue', 'warn', 'block')",
        )

        # 3. Temporary uniqueness index (FAR-1102 chunk 4, decision-records slice).
        #    Returns exactly one decision-record row per (run, gate, eval result),
        #    making repeated backfill/reconciliation sweeps idempotent for real
        #    execution outcomes; rows with eval_result_id IS NULL are unbounded
        #    by PostgreSQL semantics (NULLs compare distinct) and stay custodial.
        #    Owner: the cross-slice decision-record backfill/reconciliation
        #    reconcile-and-backfill work item (FAR-1102 decision-records track).
        #    This is a bridge index toward that owner, not a destination: policy
        #    gate decisions may legitimately repeat for the same (run, gate)
        #    across distinct eval results, so once reconciliation owns the
        #    duplicate check this index should be dropped by a follow-up
        #    migration (bridge, not destination).
        index_name = "ix_tmp_policy_gate_decisions_run_gate_result"
        inspector = sa.inspect(op.get_bind())
        existing_index_names = {ix["name"] for ix in inspector.get_indexes("policy_gate_decisions")}
        if index_name not in existing_index_names:
            op.create_index(
                index_name,
                "policy_gate_decisions",
                ["run_id", "policy_gate_id", "eval_result_id"],
                unique=True,
            )


def _drop_decision_uniqueness_index() -> None:
    """Best-effort drop of the temporary uniqueness index (idempotent)."""
    index_name = "ix_tmp_policy_gate_decisions_run_gate_result"
    inspector = sa.inspect(op.get_bind())
    existing_index_names = {ix["name"] for ix in inspector.get_indexes("policy_gate_decisions")}
    if index_name in existing_index_names:
        op.drop_index(index_name, table_name="policy_gate_decisions")


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint(
            "ck_policy_gate_decisions_resolved_action",
            "policy_gate_decisions",
            type_="check",
        )
        _drop_decision_uniqueness_index()
    op.drop_column("policy_gate_decisions", "policy_gate_version")
    op.drop_column("policy_gate_decisions", "run_id")
    op.drop_column("policy_gate_decisions", "eval_result_id")
    op.drop_column("policy_gate_decisions", "node_id")
    op.drop_column("policy_gate_decisions", "error_detail")
    op.drop_column("policy_gate_decisions", "resolved_action")
