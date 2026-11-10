"""Broaden ck_notification_delivery_log_status to allow 'in_app'.

Revision ID: 0144_broaden_notification_status_in_app
Revises: 0143_rest_connector_profile
Create Date: 2026-08-25

The error-tracking alert dispatcher (``modulo.core.error_tracking.alert_dispatcher``)
writes ``status="in_app"`` for in-app notifications, but the base
``0135_status_check_constraints`` migration pinned
``notification_delivery_log.status`` to
``('delivered', 'failed', 'dead_lettered')`` and omitted ``'in_app'``. That makes
the in-app delivery path violate the CHECK constraint at write time.

This migration drops the narrower constraint (if present) and re-creates it with
the full canonical set including ``'in_app'``. The connector_instances constraint
is left untouched (already correct in 0135).

Postgres-only, matching the repo's existing CHECK-constraint migrations.
"""

from alembic import op
from sqlalchemy import text

revision = "0144_broaden_notification_status_in_app"
down_revision = "0143_rest_connector_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE public.notification_delivery_log DROP CONSTRAINT IF EXISTS ck_notification_delivery_log_status"
        )
    )
    op.execute(
        text(
            "ALTER TABLE public.notification_delivery_log ADD CONSTRAINT "
            "ck_notification_delivery_log_status "
            "CHECK (status::text = ANY (ARRAY['delivered'::character varying, "
            "'failed'::character varying, 'dead_lettered'::character varying, "
            "'in_app'::character varying]::text[]));"
        )
    )


def downgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE public.notification_delivery_log DROP CONSTRAINT IF EXISTS ck_notification_delivery_log_status"
        )
    )
    op.execute(
        text(
            "ALTER TABLE public.notification_delivery_log ADD CONSTRAINT "
            "ck_notification_delivery_log_status "
            "CHECK (status::text = ANY (ARRAY['delivered'::character varying, "
            "'failed'::character varying, 'dead_lettered'::character varying]::text[]));"
        )
    )
