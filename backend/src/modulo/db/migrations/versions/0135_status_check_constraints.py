"""Add CHECK constraints on connector_instances.status / notification_delivery_log.status.

Revision ID: 0135_status_check_constraints
Revises: 0134_dismissals_org_rls
Create Date: 2026-08-23

Both ``connector_instances.status`` and ``notification_delivery_log.status`` are
plain ``VARCHAR`` columns (no Postgres/SQLA Enum type), so nothing currently
stops a write of an arbitrary string. This pins each to the canonical set of
values actually produced by the application:

* ``connector_instances.status`` -> ``('active', 'disabled')``
  ("active" is the server default; the graph validator treats any non-active
  value as INACTIVE, and the disable path writes "disabled".)
* ``notification_delivery_log.status`` -> ``('delivered', 'failed', 'dead_lettered')``
  ("delivered" is the server default; the notifier writes "dead_lettered" on
  final failure and "delivered" on success; the admin retry path records
  "failed".)

Postgres-only, matching the repo's existing CHECK-constraint migrations
(ck_connector_instances_*, ck_eval_definitions_type).
"""

from alembic import op
from sqlalchemy import text

revision = "0135_status_check_constraints"
down_revision = "0134_dismissals_org_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_connector_instances_status') "
            "THEN ALTER TABLE public.connector_instances ADD CONSTRAINT ck_connector_instances_status "
            "CHECK (status::text = ANY (ARRAY['active'::character varying, 'disabled'::character varying]::text[])); "
            "END IF; END $$;"
        )
    )
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_notification_delivery_log_status') "
            "THEN ALTER TABLE public.notification_delivery_log ADD CONSTRAINT ck_notification_delivery_log_status "
            "CHECK (status::text = ANY (ARRAY['delivered'::character varying, 'failed'::character varying, "
            "'dead_lettered'::character varying]::text[])); "
            "END IF; END $$;"
        )
    )


def downgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE public.notification_delivery_log DROP CONSTRAINT IF EXISTS ck_notification_delivery_log_status"
        )
    )
    op.execute(text("ALTER TABLE public.connector_instances DROP CONSTRAINT IF EXISTS ck_connector_instances_status"))
