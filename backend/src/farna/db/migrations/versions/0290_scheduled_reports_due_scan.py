"""Add due-scan index + active default for scheduled_reports.

Schema legs:

1. ``ix_scheduled_reports_due_scan`` — partial composite index
   ``(organisation_id, next_send_at) WHERE active IS TRUE AND
   next_send_at IS NOT NULL``. The every-tick due-report scan
   (``_process_due_report_scan``) selects exactly
   ``WHERE active IS TRUE AND next_send_at IS NOT NULL AND
   next_send_at <= now()`` (org scoping via RLS, same shape as the
   trigger tick scans covered by 0167/0183). Without it every tick
   falls back to a full scan past the unrelated single-column
   ``organisation_id`` / ``report_type`` indexes.

2. ``scheduled_reports.active`` server default ``true`` (Postgres
   only; SQLite/ORM-created test schemas get it from the
   ``ScheduledReport`` model's ``server_default`` via ``create_all``,
   the 0246 precedent). The column is ``NOT NULL`` with only a
   Python-side ``default=True`` (unlike ``triggers.active``, which
   carries ``server_default="true"``), so a raw-SQL INSERT omitting
   ``active`` fails with a NOT NULL violation. No data leg: the
   column is already NOT NULL, so no NULLs can exist; SET DEFAULT
   only affects future inserts.

Downgrade: drops the index and removes the column default.

Revision ID: 0290_scheduled_reports_due_scan
Revises: 0289_pipelines_environment_profile
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0290_scheduled_reports_due_scan"
down_revision: str | None = "0289_pipelines_environment_profile"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.create_index(
        "ix_scheduled_reports_due_scan",
        "scheduled_reports",
        ["organisation_id", "next_send_at"],
        postgresql_where=sa.text("active IS TRUE AND next_send_at IS NOT NULL"),
    )
    if not _is_postgres():
        return
    op.get_bind().execute(sa.text("ALTER TABLE scheduled_reports ALTER COLUMN active SET DEFAULT true"))


def downgrade() -> None:
    if _is_postgres():
        op.get_bind().execute(sa.text("ALTER TABLE scheduled_reports ALTER COLUMN active DROP DEFAULT"))
    op.drop_index("ix_scheduled_reports_due_scan", table_name="scheduled_reports")
