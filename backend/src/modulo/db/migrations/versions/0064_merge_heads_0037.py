"""Merge the two 0037 migration heads back into a single chain.

Revision ID: 0064_merge_heads_0037
Revises: 0037_add_scheduled_reports_created_by, 0037_break_glass_enforcement
Create Date: 2026-08-03

Both 0037 migrations branched off ``0036_break_glass_columns`` at the same
time and both left the migration chain with two heads:

- ``0037_add_scheduled_reports_created_by`` — scheduled_reports.created_by
  schema drift reconciliation (#615).
- ``0037_break_glass_enforcement`` — accounts UPDATE allow-list +
  modulo_breakglass grants, deliverable B (#612).

This is a pure graph merge: each 0037 migration already applied its own
schema changes independently, so ``upgrade()`` is a no-op. Joining them
restores a single head so ``alembic upgrade heads`` works again (previously
it raised "Multiple head revisions are present", blocking app startup and
the deploy gate).
"""

from __future__ import annotations

import sqlalchemy as sa

revision = "0064_merge_heads_0037"
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
