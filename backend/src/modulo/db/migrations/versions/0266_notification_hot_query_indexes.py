"""improve-database(notification): TTL + visibility + dismissal lookup indexes.

Revision ID: 0266_notification_hot_query_indexes
Revises: 0265_hitl_review_window
Create Date: 2026-09-29

Covers ``backend/src/modulo/db/models/notification.py`` (notifications,
dismissals, notification_preferences), an area not recently reviewed. Earlier
passes already added ``(organisation_id, created_at)``,
``(organisation_id, level, scope, category)`` (0155) and
``(dismissed_by_user_id)`` (0152); the four gaps below remain, each backed by
a hot read path in ``db/crud/notifications.py``:

* ``ix_notifications_org_expires_at`` — every live view (dashboard, unread
  badge, inbox default, ``status=active``) filters
  ``organisation_id = $1 AND (expires_at IS NULL OR expires_at > now())``;
  the TTL predicate had no supporting index.
* ``ix_notifications_org_scope_target`` — the visibility clause
  ``scope='org' OR scope='admin' OR (scope='user' AND target_user_id=$1)``
  is evaluated per org on every list/count; the composite lets Postgres
  bitmap-serve the per-user arm instead of filtering on the single-column
  ``target_user_id`` index.
* ``ix_dismissals_org_user_scope`` — the active-view anti-join correlates on
  ``dismissals.organisation_id = notifications.organisation_id AND
  (dismissed_by_user_id=$1 OR dismiss_scope='scope')``; only single-column
  indexes existed.
* ``ix_dismissals_user_scope`` — the ``dismissed_self`` / ``dismissed_scope``
  historical filters query ``dismissed_by_user_id=$1 AND dismiss_scope=$2``
  with no org predicate; the pre-existing unique index leads on
  ``notification_id`` and cannot serve it.

``notification_preferences`` needs nothing: all reads filter on
``(organisation_id, account_id)`` which the
``uq_notification_preferences_org_account_category`` unique index already
covers. RLS is present on all three tables (0003/0110/0115/0134); every new
index leads on the tenant column where the query is tenant-scoped.

Additive indexes only — no column/table changes. Same
``CREATE INDEX IF NOT EXISTS`` pattern as 0128/0155 (Alembic wraps each
revision in a transaction, so ``CONCURRENTLY`` is unavailable).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0266_notification_hot_query_indexes"
down_revision: str | None = "0265_hitl_review_window"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEXES = [
    (
        "ix_notifications_org_expires_at",
        (
            "CREATE INDEX IF NOT EXISTS ix_notifications_org_expires_at "
            'ON public."notifications" (organisation_id, expires_at);'
        ),
    ),
    (
        "ix_notifications_org_scope_target",
        (
            "CREATE INDEX IF NOT EXISTS ix_notifications_org_scope_target "
            'ON public."notifications" (organisation_id, scope, target_user_id);'
        ),
    ),
    (
        "ix_dismissals_org_user_scope",
        (
            "CREATE INDEX IF NOT EXISTS ix_dismissals_org_user_scope "
            'ON public."dismissals" (organisation_id, dismissed_by_user_id, dismiss_scope);'
        ),
    ),
    (
        "ix_dismissals_user_scope",
        (
            "CREATE INDEX IF NOT EXISTS ix_dismissals_user_scope "
            'ON public."dismissals" (dismissed_by_user_id, dismiss_scope);'
        ),
    ),
]


_DROPS = [
    "DROP INDEX IF EXISTS ix_notifications_org_expires_at;",
    "DROP INDEX IF EXISTS ix_notifications_org_scope_target;",
    "DROP INDEX IF EXISTS ix_dismissals_org_user_scope;",
    "DROP INDEX IF EXISTS ix_dismissals_user_scope;",
]


def upgrade() -> None:
    bind = op.get_bind()
    for _name, stmt in _INDEXES:
        bind.execute(text(stmt))


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in _DROPS:
        bind.execute(text(stmt))
