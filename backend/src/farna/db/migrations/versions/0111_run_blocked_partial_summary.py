"""Add ``runs.blocked_partial_summary`` (FAR-213).

Revision ID: 0111_run_blocked_partial_summary
Revises: 0110_schema_pipeline_runtime
Create Date: 2026-08-16

FAR-213 run-termination compensation persists a structured record when a run
terminalizes ``eval_failed``/``eval_blocked`` from a guardrail block: executed
nodes (in order), per-node publish status (published/compensated/not-compensated),
output references (the record references the run's ``outputs_json`` by
``{run_id, node_id}`` — it never duplicates raw payloads), and per-attempt
compensation outcomes. The record is a single nullable JSONB column
``blocked_partial_summary`` on ``runs`` — UNIQUE(run_id) is inherent (run_id is
the PK), and the write is an upsert/refresh so a guardrail-override re-dispatch
that re-blocks overwrites the stale verdict (the same pattern as the FAR-189
``run_classification`` column).

The ORM model maps the column as generic JSON for SQLite/MariaDB parity (the
``run_classification`` precedent, migration 0100). No index: the column is read
only by run-detail (keyed by the runs PK), never swept by a column predicate.

Downgrade drops the column (additive, nullable, never backfilled — safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0111_run_blocked_partial_summary"
down_revision: str | None = "0110_schema_pipeline_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("blocked_partial_summary", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "blocked_partial_summary")
