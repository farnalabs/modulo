"""Merge the two parallel 0037 migration heads into a single chain.

Revision ID: 0038_merge_heads
Revises: 0037_add_scheduled_reports_created_by, 0037_break_glass_enforcement
Create Date: 2026-08-03

Two parallel 0037 migrations branched off ``0036_break_glass_columns``:

- ``0037_add_scheduled_reports_created_by`` (PR #615) — scheduled-reports drift fix.
- ``0037_break_glass_enforcement`` (PR #612) — break-glass enforcement columns.

Both chained from the same parent, producing two alembic heads and failing CI's
migration-heads check on every PR.

This is a pure graph merge: both branches' schema changes are already applied by
their own migrations and are independent, so ``upgrade()`` is a no-op. Joining
them restores a single head so ``alembic upgrade head`` works again (previously
it raised "Multiple head revisions are present", blocking app startup and the
deploy gate).
"""

from __future__ import annotations

import sqlalchemy as sa

revision = "0038_merge_heads"
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
