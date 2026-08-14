"""Add the ``run_classification`` column to runs (FAR-189)

Revision ID: 0100_run_classification
Revises: 0099_run_raw_output_markers
Create Date: 2026-08-14

FAR-189 persists a run-outcome classification record when a run reaches a
terminal status, so the ongoing-trigger streak engine (FAR-190) can query
classification records instead of raw run status. The record is a single
nullable JSONB column ``run_classification`` on ``runs`` with shape
``{value, reason, delivered_pr_urls, computed_at, work_intact,
declared_success_nodes}`` — UNIQUE(run_id) is inherent (run_id is the PK), and
the write is an upsert/refresh so re-terminalization (a retry policy re-flips a
classified run back to pending, then re-runs) overwrites the stale verdict.

The ORM model maps the column as generic JSON for SQLite/MariaDB parity (the
``raw_output_markers`` precedent, migration 0099). No index: the record is
written once per terminalization and read by the streak engine per run — not
queried in bulk.

Downgrade drops the column (additive, nullable, never backfilled — safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0100_run_classification"
down_revision: str | None = "0099_run_raw_output_markers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("run_classification", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "run_classification")
