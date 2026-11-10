"""Add positive-range CHECK constraints on numeric columns.

Revision ID: 0153_add_numeric_check_constraints
Revises: 0152_dismissed_by_user_id_index
Create Date: 2026-08-27

Several numeric columns can hold semantically invalid values that the
application never intends to write but the database does not currently
reject:

* ``connector_profiles.timeout_seconds`` — an HTTP timeout must be strictly
  positive; a zero or negative timeout is a misconfiguration that would
  fail at request time. Guard with ``> 0`` (nullable column stays NULL-able).
* ``connector_profiles.response_max_bytes`` — a max response size must be
  positive when set.
* ``web_vital_events.metric_value`` — Web Vitals metrics (LCP, CLS, INP, ...)
  are non-negative physical measurements; a negative value is corrupt data.

Each constraint is added idempotently (only when absent) so the migration is
safe to re-run and will not clobber a manually-tightened definition. These are
additive and backward-compatible: pre-existing rows that already satisfy the
constraint (the normal case) are unaffected, and the constraints only reject
future invalid writes.
"""

from __future__ import annotations

from alembic import op

revision: str = "0153_add_numeric_check_constraints"
down_revision: str | None = "0152_dismissed_by_user_id_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname="
        "'ck_connector_profiles_timeout_positive') THEN "
        "ALTER TABLE public.connector_profiles ADD CONSTRAINT "
        "ck_connector_profiles_timeout_positive "
        "CHECK (timeout_seconds IS NULL OR timeout_seconds > 0); "
        "END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname="
        "'ck_connector_profiles_response_max_bytes_positive') THEN "
        "ALTER TABLE public.connector_profiles ADD CONSTRAINT "
        "ck_connector_profiles_response_max_bytes_positive "
        "CHECK (response_max_bytes IS NULL OR response_max_bytes > 0); "
        "END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname="
        "'ck_web_vital_events_metric_value_nonneg') THEN "
        "ALTER TABLE public.web_vital_events ADD CONSTRAINT "
        "ck_web_vital_events_metric_value_nonneg "
        "CHECK (metric_value >= 0); "
        "END IF; END $$;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.web_vital_events DROP CONSTRAINT IF EXISTS ck_web_vital_events_metric_value_nonneg;")
    op.execute(
        "ALTER TABLE public.connector_profiles "
        "DROP CONSTRAINT IF EXISTS ck_connector_profiles_response_max_bytes_positive;"
    )
    op.execute(
        "ALTER TABLE public.connector_profiles DROP CONSTRAINT IF EXISTS ck_connector_profiles_timeout_positive;"
    )
