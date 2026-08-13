"""Add run_evidence table + runs.work_intact (FAR-152 evidence & no-op detection)

Revision ID: 0091_run_evidence
Revises: 0090_add_budget_exceeded_status
Create Date: 2026-08-12

Implements the §15.3/§15.12 schema for the tri-state evidence machinery:

- ``run_evidence (run_id FK, node_id, evidence_state, evidence_detail,
  evidence_written_at, UNIQUE(run_id, node_id))`` — one row per node written
  by the post-commit async evidence probe / reconciliation sweep. Deliberately
  NOT org-scoped (no ``organisation_id`` column): rows are written and read
  only by the harness probe machinery with an explicit run_id.
- ``runs.work_intact BOOL NULL`` — computed at terminalization from
  completed-node artifacts + full DAG ran (restores the false-failure banner
  for incidents #1/#3). NULL = unknown/not-applicable, never retro-false.

NOTE on migration-chain coordination: this migration chains after
0090 (add_budget_exceeded_status, the current head on main); 0090 itself
chains through 0089 -> 0088 -> 0087_circuit_breaker -> 0086 -> 0085,
so the final head is 0091 and the chain is linear (0085 -> ... -> 0091).

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0091_run_evidence"
down_revision: str | None = "0090_add_budget_exceeded_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "run_evidence"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("work_intact", sa.Boolean(), nullable=True),
    )
    op.create_table(
        _TABLE,
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("evidence_state", sa.String(length=20), nullable=False),
        sa.Column("evidence_detail", sa.String(length=2000), nullable=True),
        sa.Column(
            "evidence_written_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "node_id", name="pk_run_evidence_run_node"),
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
    op.drop_column("runs", "work_intact")
