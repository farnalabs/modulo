"""Add node_attempt_count to runs

Revision ID: 0073_run_node_attempt_count
Revises: 0072_sync_feature_flag_catalog
Create Date: 2026-08-09

Adds ``node_attempt_count`` to the ``runs`` table. This counter tracks REAL
node-execution attempts (incremented post capacity-check, pre-stream in
``PipelineExecutor.execute``) and bounds the ``NodeCancelledError`` retry
budget in the executor. It is distinct from ``claim_count``, which increments
on EVERY SAQ claim — including claims that never execute (capacity-deferral
demotions, pre-node setup failures) — so those could exhaust the retry budget
before any real execution attempt (postmortem FAR-121).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073_run_node_attempt_count"
down_revision: str | None = "0072_sync_feature_flag_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("node_attempt_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("runs", "node_attempt_count")
