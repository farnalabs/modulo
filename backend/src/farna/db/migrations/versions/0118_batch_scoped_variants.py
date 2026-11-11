"""FAR-332 — batch-scoped variant comparison columns (runs).

Revision ID: 0118_batch_scoped_variants
Revises: 0117_toctou_hardening
Create Date: 2026-08-20

Two additive, nullable columns on ``runs`` back the batch-scoped variant
comparison workflow:

1. ``runs.batch_id`` — every run fired together by ``run_variant_batch`` is
   stamped with the same ``batch_id`` so the compare route can load a batch's
   runs purely by this key (independent of the live variant group, which may
   be soft-deleted later). Null for legacy and non-variant runs; no backfill.

2. ``runs.variant_config_snapshot`` — a frozen JSON snapshot of the variant's
   ``{variant_id, variant_name, snapshot_id, run_context_overrides}`` captured
   at fire time. The compare view reads this, never the live snapshot.

A composite ``(variant_group_id, batch_id)`` index backs the batch compare read.
Generic JSON (not JSONB) keeps SQLite/MariaDB parity with the other
``runs.*_json`` columns (the ``run_classification`` precedent).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0118_batch_scoped_variants"
down_revision: str | None = "0117_toctou_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("batch_id", sa.Uuid(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("variant_config_snapshot", sa.JSON(none_as_null=True), nullable=True),
    )
    op.execute("CREATE INDEX ix_runs_variant_group_batch ON runs (variant_group_id, batch_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_variant_group_batch")
    op.drop_column("runs", "variant_config_snapshot")
    op.drop_column("runs", "batch_id")
