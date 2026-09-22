"""FAR-902: per-attempt schema enforcement telemetry.

Adds ``schema_enforcement_json`` (JSONB on Postgres, JSON on ORM) to
``run_node_outputs`` — the per-node output store keyed by
``(run_id, node_id, attempt_key)``.  Carries the per-attempt
``SchemaEnforcementRecord`` payload (outcome, profile, native-vs-verbatim,
repair/wasted counts, validation errors).

CHECK constraint: ``attempt_key <> '__final__' OR
schema_enforcement_json IS NULL`` — terminal ``__final__`` rows have no
enforcement record.

Partial index: ``ix_run_node_outputs_enforcement_pending`` on
``run_id`` WHERE ``schema_enforcement_json IS NOT NULL`` — used by the
analytics compensating sweep to find runs with enforcement data that need
aggregate counter updates (``dispatcher_reconcile`` 60s path).

Downgrade: drops the column, CHECK constraint, and partial index.

Revision ID: 0251_schema_enforcement_telemetry
Revises: 0250_eval_policy_gate
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0251_schema_enforcement_telemetry"
down_revision = "0250_eval_policy_gate"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns(table)}
    return column in columns


def upgrade() -> None:
    if not _is_postgres():
        return
    if _has_column("run_node_outputs", "schema_enforcement_json"):
        return

    # JSONB on Postgres (repo parity: ORM carries JSON, migration promotes).
    op.add_column(
        "run_node_outputs",
        sa.Column("schema_enforcement_json", sa.JSON(), nullable=True),
    )
    # CHECK: terminal __final__ rows must not carry enforcement data.
    op.create_check_constraint(
        "ck_run_node_outputs_final_no_enforcement",
        "run_node_outputs",
        "attempt_key <> '__final__' OR schema_enforcement_json IS NULL",
    )
    # Partial index for the analytics compensating sweep predicate:
    # "find enforcement records on non-final attempt rows".
    # The sweep query filter is:
    #   SELECT ... FROM run_node_outputs
    #   WHERE schema_enforcement_json IS NOT NULL
    #     AND attempt_key <> '__final__'
    # The partial index predicate matches exactly.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_run_node_outputs_enforcement_pending
        ON run_node_outputs (run_id)
        WHERE schema_enforcement_json IS NOT NULL
          AND attempt_key <> '__final__'
        """
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.drop_index("ix_run_node_outputs_enforcement_pending", table_name="run_node_outputs")
    op.drop_constraint(
        "ck_run_node_outputs_final_no_enforcement",
        "run_node_outputs",
        type_="check",
    )
    op.drop_column("run_node_outputs", "schema_enforcement_json")
