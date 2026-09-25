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


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint(
            "ck_policy_gate_decisions_resolved_action",
            "policy_gate_decisions",
            type_="check",
        )
    op.drop_column("policy_gate_decisions", "policy_gate_version")
    op.drop_column("policy_gate_decisions", "run_id")
    op.drop_column("policy_gate_decisions", "eval_result_id")
    op.drop_column("policy_gate_decisions", "node_id")
    op.drop_column("policy_gate_decisions", "error_detail")
    op.drop_column("policy_gate_decisions", "resolved_action")
