"""Merge the 0037 scheduled_reports.created_by and break-glass enforcement heads.

Revision ID: 7ced234e1a91
Revises: 0037_add_scheduled_reports_created_by, 0037_break_glass_enforcement
Create Date: 2026-08-03 19:03:18.055294

Two independent 0037 migrations branched off ``0036_break_glass_columns``:

- ``0037_add_scheduled_reports_created_by`` reconciles the
  ``scheduled_reports.created_by`` schema drift across deployments.
- ``0037_break_glass_enforcement`` applies the break-glass deliverable (B)
  accounts UPDATE allow-list and ``modulo_breakglass`` grants.

Both are additive and independent, so this is a pure graph merge with a no-op
``upgrade()``. Joining them restores a single head so ``alembic upgrade head``
works again and the "Check migration heads" CI gate passes.
"""

from collections.abc import Sequence

revision: str = "7ced234e1a91"
down_revision: str | Sequence[str] | None = (
    "0037_add_scheduled_reports_created_by",
    "0037_break_glass_enforcement",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
