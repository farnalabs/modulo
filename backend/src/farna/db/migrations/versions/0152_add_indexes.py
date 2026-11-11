"""improve-database: index on dismissals.dismissed_by_user_id.

The only index covering ``dismissed_by_user_id`` is the composite
``uq_dismissal_user_notification(notification_id, dismissed_by_user_id)`` where
it is the second column, so a lookup of all dismissals for a given user
(``WHERE dismissed_by_user_id = ?``) cannot use it. Add a standalone index.

Revision ID: 0152_dismissed_by_user_id_index
Revises: 0151_fix_constraints
"""

from __future__ import annotations

from alembic import op

revision: str = "0152_dismissed_by_user_id_index"
down_revision: str | None = "0151_fix_constraints"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_dismissals_dismissed_by_user_id",
        "dismissals",
        ["dismissed_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dismissals_dismissed_by_user_id",
        table_name="dismissals",
    )
