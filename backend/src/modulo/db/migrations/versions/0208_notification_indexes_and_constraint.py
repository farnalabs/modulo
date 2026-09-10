"""Add composite indexes for notification query patterns; fix CHECK constraint format.

Revision ID: 0208_notification_indexes_and_constraint
Revises: 0207_collection_install_tracking
Create Date: 2026-09-10

Findings from improve-database lens pass over the notification model cluster:

1. **Missing composite indexes** — the CRUD layer filters notifications by
   (organisation_id, level) and (organisation_id, category); delivery log by
   (organisation_id, event_type); dismissals by (dismissed_by_user_id,
   notification_id). Without covering composites the planner must scan
   per-org rows and apply the second predicate in a filter.

2. **CHECK constraint format drift** — migration 0144 defined the status
   CHECK as ``status::text = ANY (ARRAY[...]::text[])`` while the ORM
   model uses ``IN (...)``. Alembic autogenerate detects drift and
   proposes dropping/recreating on every CI run. Aligning to the IN
   form silences the false-positive.

All operations are additive / constraint-replace on existing tables.
No new tables, columns, or relationships.
"""

from alembic import op
import sqlalchemy as sa


revision = "0208_notification_indexes_and_constraint"
down_revision = "0207_collection_install_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Composite indexes for hot query paths ---

    # notifications: filter by (org, level) in dashboard + unread count
    op.create_index(
        "ix_notifications_org_level",
        "notifications",
        ["organisation_id", "level"],
    )

    # notifications: filter by (org, category) in list + count queries
    op.create_index(
        "ix_notifications_org_category",
        "notifications",
        ["organisation_id", "category"],
    )

    # dismissals: "is this dismissed by user?" subquery
    op.create_index(
        "ix_dismissals_user_notification",
        "dismissals",
        ["dismissed_by_user_id", "notification_id"],
    )

    # notification_delivery_log: filter by (org, event_type)
    op.create_index(
        "ix_notification_delivery_log_org_event",
        "notification_delivery_log",
        ["organisation_id", "event_type"],
    )

    # --- CHECK constraint format alignment ---

    op.execute(
        sa.text(
            "ALTER TABLE public.notification_delivery_log "
            "DROP CONSTRAINT IF EXISTS ck_notification_delivery_log_status"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE public.notification_delivery_log "
            "ADD CONSTRAINT ck_notification_delivery_log_status "
            "CHECK (status IN ('delivered', 'failed', 'dead_lettered', 'in_app'))"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_notification_delivery_log_org_event", table_name="notification_delivery_log")
    op.drop_index("ix_dismissals_user_notification", table_name="dismissals")
    op.drop_index("ix_notifications_org_category", table_name="notifications")
    op.drop_index("ix_notifications_org_level", table_name="notifications")

    # Restore the original ARRAY-form CHECK constraint from migration 0144
    op.execute(
        sa.text(
            "ALTER TABLE public.notification_delivery_log "
            "DROP CONSTRAINT IF EXISTS ck_notification_delivery_log_status"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE public.notification_delivery_log "
            "ADD CONSTRAINT ck_notification_delivery_log_status "
            "CHECK (status::text = ANY (ARRAY["
            "'delivered'::character varying, "
            "'failed'::character varying, "
            "'dead_lettered'::character varying, "
            "'in_app'::character varying"
            "]::text[]))"
        )
    )
