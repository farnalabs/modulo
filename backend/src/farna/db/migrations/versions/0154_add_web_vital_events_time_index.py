"""web_vital_events — add (organisation_id, recorded_at) time-range index.

Revision ID: 0154_add_web_vital_events_time_index
Revises: 0153_add_numeric_check_constraints
Create Date: 2026-08-27

The metrics dashboard queries ``web_vital_events`` by organisation (enforced
by RLS on ``organisation_id``) and a ``recorded_at`` window, optionally
filtered by ``metric_name`` (see ``api/routes/metrics.py``). ``OrgScoped``
indexes ``organisation_id`` alone and ``metric_name`` is indexed alone, but
there is no composite index covering the dominant (organisation_id,
recorded_at) range scan, forcing a wider filter/seqscan on read-heavy
dashboard pages.

This mirrors the blocking ``CREATE INDEX IF NOT EXISTS`` pattern established
in 0128 (Alembic wraps every revision in one transaction, so
``CREATE INDEX CONCURRENTLY`` is unavailable here and would fail with
``relation does not exist`` against the uncommitted schema). ``IF NOT EXISTS``
keeps the migration idempotent and re-runnable.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0154_add_web_vital_events_time_index"
down_revision: str | None = "0153_add_numeric_check_constraints"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_web_vital_events_org_recorded "
            "ON public.web_vital_events (organisation_id, recorded_at);"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP INDEX IF EXISTS ix_web_vital_events_org_recorded;"))
