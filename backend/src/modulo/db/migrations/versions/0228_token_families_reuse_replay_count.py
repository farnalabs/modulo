"""Add per-window reuse replay counter to token families.

Revision ID: 0228_token_families_reuse_replay_count
Revises: 0227_token_families_refresh_grace
Create Date: 2026-09-14

Adds ``reuse_replay_count`` (integer, NOT NULL default 0) to ``token_families``
so the FAR-819 reuse grace window can enforce its per-window replay budget
(``REFRESH_REUSE_GRACE_MAX_PER_WINDOW``). The window start is already tracked
by ``reuse_window_started_at``, but counting the replays admitted within the
current window requires a persisted counter distinct from the window marker.
"""

import sqlalchemy as sa
from alembic import op

revision = "0228_token_families_reuse_replay_count"
down_revision = "0227_token_families_refresh_grace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "token_families",
        sa.Column("reuse_replay_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("token_families", "reuse_replay_count")
