"""Merge the two 0037-mergepoint heads back into a single chain.

Revision ID: 22af0b76c0f4
Revises: 0064_merge_heads_0037, 7ced234e1a91
Create Date: 2026-08-03

``0064_merge_heads_0037`` (main) and ``7ced234e1a91`` (branch-fixer staging)
both merge the two 0037 migrations (``0037_add_scheduled_reports_created_by``
and ``0037_break_glass_enforcement``). Each is a pure graph merge with a no-op
``upgrade()``; joining them restores a single head so ``alembic upgrade heads``
and the "Check migration heads" CI gate pass.
"""

import sqlalchemy as sa

revision: str = "22af0b76c0f4"
down_revision: str | sa.Sequence[str] | None = (
    "0064_merge_heads_0037",
    "7ced234e1a91",
)
branch_labels: str | sa.Sequence[str] | None = None
depends_on: str | sa.Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
