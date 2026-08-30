"""Add hot query indexes for list/status scans (improve-database lenses).

Revision ID: 0155_add_hot_query_indexes
Revises: 0154_add_web_vital_events_time_index
Create Date: 2026-08-30

Several high-traffic list endpoints filter or order by columns that have no
supporting composite index, forcing full scans / sorts:

* ``runs`` is listed by (organisation_id, status) and (pipeline_id, status);
  the dispatcher also sweeps (status IN ('pending','running')) by heartbeat.
* ``error_groups`` is listed by (organisation_id, status) ordered by
  ``last_seen``.
* ``error_events`` is listed by (organisation_id, status) ordered by
  ``created_at``; the existing index leads on ``fingerprint`` so it cannot
  serve these.
* ``audit_events`` is listed by (organisation_id, created_at) and looked up by
  (resource_type, resource_id).
* ``notifications`` is listed by (organisation_id, created_at) and filtered by
  (level, scope, category).
* ``chat_messages`` is listed by (session_id, created_at).

Indexed with the same ``CREATE INDEX IF NOT EXISTS`` pattern as 0128/0154
(Alembic wraps each revision in a transaction, so ``CONCURRENTLY`` is
unavailable and would fail).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0155_add_hot_query_indexes"
down_revision: str | None = "0154_add_web_vital_events_time_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEXES = [
    (
        "ix_runs_organisation_status",
        'CREATE INDEX IF NOT EXISTS ix_runs_organisation_status ON public."runs" (organisation_id, status);',
    ),
    (
        "ix_runs_pipeline_status",
        'CREATE INDEX IF NOT EXISTS ix_runs_pipeline_status ON public."runs" (pipeline_id, status);',
    ),
    (
        "ix_runs_status_heartbeat",
        "CREATE INDEX IF NOT EXISTS ix_runs_status_heartbeat "
        'ON public."runs" (status, heartbeat_at) '
        "WHERE status IN ('pending', 'running');",
    ),
    (
        "ix_error_groups_org_status_last_seen",
        "CREATE INDEX IF NOT EXISTS ix_error_groups_org_status_last_seen "
        'ON public."error_groups" (organisation_id, status, last_seen);',
    ),
    (
        "ix_error_events_org_created_at",
        "CREATE INDEX IF NOT EXISTS ix_error_events_org_created_at "
        'ON public."error_events" (organisation_id, created_at);',
    ),
    (
        "ix_error_events_org_status",
        'CREATE INDEX IF NOT EXISTS ix_error_events_org_status ON public."error_events" (organisation_id, status);',
    ),
    (
        "ix_audit_events_org_created_at",
        "CREATE INDEX IF NOT EXISTS ix_audit_events_org_created_at "
        'ON public."audit_events" (organisation_id, created_at);',
    ),
    (
        "ix_audit_events_resource",
        'CREATE INDEX IF NOT EXISTS ix_audit_events_resource ON public."audit_events" (resource_type, resource_id);',
    ),
    (
        "ix_notifications_org_created_at",
        "CREATE INDEX IF NOT EXISTS ix_notifications_org_created_at "
        'ON public."notifications" (organisation_id, created_at);',
    ),
    (
        "ix_notifications_org_level_scope_category",
        "CREATE INDEX IF NOT EXISTS ix_notifications_org_level_scope_category "
        'ON public."notifications" (organisation_id, level, scope, category);',
    ),
    (
        "ix_chat_messages_session_created",
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_session_created "
        'ON public."chat_messages" (session_id, created_at);',
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    for _name, stmt in _INDEXES:
        bind.execute(text(stmt))


def downgrade() -> None:
    bind = op.get_bind()
    for name, _stmt in _INDEXES:
        bind.execute(text(f"DROP INDEX IF EXISTS {name};"))
