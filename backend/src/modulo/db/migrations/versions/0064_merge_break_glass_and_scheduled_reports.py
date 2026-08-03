"""Merge break-glass enforcement and scheduled_reports heads into a single chain.

Revision ID: 0064_merge_break_glass_and_scheduled_reports
Revises: 0037_break_glass_enforcement, 0037_add_scheduled_reports_created_by
Create Date: 2026-08-03

Two parallel branches both chained off ``0036_break_glass_columns``:

- ``0037_break_glass_enforcement`` (deliverable B): accounts UPDATE allow-list
  DDL + ``modulo_breakglass`` grants.
- ``0037_add_scheduled_reports_created_by`` (from main): idempotent
  reconciliation of the ``scheduled_reports.created_by`` column drift.

Both are additive and independent, so ``upgrade()`` is a no-op. Joining them
restores a single head so ``alembic upgrade head`` works again.
"""

from __future__ import annotations

import sqlalchemy as sa

revision = "0064_merge_break_glass_and_scheduled_reports"
down_revision: str | sa.Sequence[str] | None = (
    "0037_break_glass_enforcement",
    "0037_add_scheduled_reports_created_by",
)
branch_labels: str | sa.Sequence[str] | None = None
depends_on: str | sa.Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
