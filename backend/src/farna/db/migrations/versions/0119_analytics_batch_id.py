"""FAR-332 — analytics batch_id dimension (run_daily_facts).

Revision ID: 0119_analytics_batch_id
Revises: 0118_batch_scoped_variants
Create Date: 2026-08-20

The batch-scoped variant comparison tags every run fired together by
``run_variant_batch`` with the same ``runs.batch_id``. The analytics facts
table carries the rest of the run dimensions but not ``batch_id``, so
"experiment-run tagging (analytics filterable)" was a no-op. This adds a
nullable ``batch_id`` column to ``run_daily_facts`` so the facts become
filterable/groupable by batch.

Null for legacy and non-variant runs; no backfill (pre-alpha). ``batch_id`` is
deliberately NOT a FK to ``runs`` — facts must survive the 90-day run purge
(ADR 020), matching ``run_id`` / ``parent_run_id`` / ``snapshot_id``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0119_analytics_batch_id"
down_revision: str | None = "0118_batch_scoped_variants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_daily_facts", sa.Column("batch_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_daily_facts", "batch_id")
