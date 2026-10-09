"""Add server default 0 for audit_chain_heads.event_count.

Schema leg:

``audit_chain_heads.event_count`` is ``NOT NULL`` with only a Python-side
``default=0`` (unlike its sibling counters ``daily_run_count.run_count``,
``eval_suite_run.total_cases`` and ``cost_component.sort_order``, which all
carry ``server_default="0"``), so a raw-SQL INSERT omitting ``event_count``
fails with a NOT NULL violation. The column default is set to ``0``
(Postgres only; SQLite/ORM-created test schemas get it from the
``AuditChainHead`` model's ``server_default`` via ``create_all``, the 0246
precedent). No data leg: the column is already NOT NULL, so no NULLs can
exist; SET DEFAULT only affects future inserts.

Downgrade: removes the column default.

Revision ID: 0291_audit_chain_event_count_default
Revises: 0290_scheduled_reports_due_scan
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0291_audit_chain_event_count_default"
down_revision: str | None = "0290_scheduled_reports_due_scan"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return
    op.get_bind().execute(sa.text("ALTER TABLE audit_chain_heads ALTER COLUMN event_count SET DEFAULT 0"))


def downgrade() -> None:
    if not _is_postgres():
        return
    op.get_bind().execute(sa.text("ALTER TABLE audit_chain_heads ALTER COLUMN event_count DROP DEFAULT"))
