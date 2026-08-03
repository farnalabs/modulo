"""Merge the two 0037 migration heads into a single chain.

Revision ID: 0037_merge_heads
Revises: 0037_add_scheduled_reports_created_by, 0037_break_glass_enforcement
Create Date: 2026-08-03

Two independent deliverable branches both descended from
``0036_break_glass_columns`` and accumulated as parallel heads:

- ``0037_break_glass_enforcement`` (PR #612, deliverable B): accounts UPDATE
  allow-list + ``modulo_breakglass`` column/table grants.
- ``0037_add_scheduled_reports_created_by`` (PR #615): idempotent
  ``scheduled_reports.created_by`` column/index reconciliation.

This is a pure graph merge: both branches' schema changes are additive and
independent, so ``upgrade()`` is a no-op. Joining them restores a single head
so ``alembic upgrade head`` and the CI migration-heads gate pass again.
"""

from __future__ import annotations

import sqlalchemy as sa

revision = "0037_merge_heads"
down_revision: str | sa.Sequence[str] | None = (
    "0037_add_scheduled_reports_created_by",
    "0037_break_glass_enforcement",
)
branch_labels: str | sa.Sequence[str] | None = None
depends_on: str | sa.Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
