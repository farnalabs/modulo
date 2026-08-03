"""Merge the two 0037 migration heads into a single chain.

Revision ID: 0037_merge_all_heads
Revises: 0037_add_scheduled_reports_created_by, 0037_break_glass_enforcement
Create Date: 2026-08-03

Two parallel PRs both branched their ``0037_*`` migration off
``0036_break_glass_columns``:

- ``0037_add_scheduled_reports_created_by`` (reconcile scheduled_reports
  ``created_by`` schema drift; guarded column + index add).
- ``0037_break_glass_enforcement`` (accounts UPDATE allow-list + modulo_breakglass
  grants).

The two chains touch disjoint objects, so ``upgrade()`` is a no-op. Joining them
restores a single head so ``alembic upgrade head`` works again (previously it
raised "Multiple head revisions are present", blocking app startup and the
merge-queue hard gate).
"""

from collections.abc import Sequence

revision: str = "0037_merge_all_heads"
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
