"""Add ``feedback_records.correction_state`` (FAR-210).

Revision ID: 0112_feedback_correction_state
Revises: 0111_run_blocked_partial_summary
Create Date: 2026-08-17

FAR-210 T2b single-node self-correction persists an idempotency key and prior
fingerprints on the FeedbackRecord so an interrupted correction can resume by
RE-VALIDATING the produced output (never re-running the LM) and so the
convergence check has recorded prior states. The record is a single nullable
JSONB column ``correction_state`` on ``feedback_records`` — idempotency_key,
input/output fingerprints, attempt counter.

The ORM model maps the column as generic JSON for SQLite/MariaDB parity (the
``blocked_partial_summary`` precedent, migration 0111). No index: the column is
read only by run-keyed feedback lookups, never swept by a column predicate.

Downgrade drops the column (additive, nullable, never backfilled — safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0112_feedback_correction_state"
down_revision: str | None = "0111_run_blocked_partial_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("feedback_records", sa.Column("correction_state", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("feedback_records", "correction_state")
