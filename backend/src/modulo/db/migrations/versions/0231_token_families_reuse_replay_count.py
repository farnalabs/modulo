"""No-op chain link for the removed per-window reuse replay counter.

Revision ID: 0231_token_families_reuse_replay_count
Revises: 0230_token_families_refresh_grace
Create Date: 2026-09-14

FAR-819 v6 (PR #542) superseded the original no-mint design: the
``reuse_window_started_at`` and ``reuse_replay_count`` columns introduced by
PR #502 (main's 0230/0231) were removed, leaving only ``rotated_at``.
This revision id is kept as a **no-op chain link** so that any environment
which already ran ``alembic upgrade head`` on main (and therefore has
``alembic_version = '0231_token_families_reuse_replay_count'``) can still
locate the revision and run a harmless upgrade/downgrade. A squashing
migration will clean up the orphaned ``reuse_replay_count`` column left on
already-migrated databases.
"""

revision = "0231_token_families_reuse_replay_count"
down_revision = "0230_token_families_refresh_grace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
