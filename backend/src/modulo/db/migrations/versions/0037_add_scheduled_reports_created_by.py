"""Reconcile scheduled_reports.created_by schema drift across deployments.

Revision ID: 0037_add_scheduled_reports_created_by
Revises: 0036_break_glass_columns
Create Date: 2026-08-03

Deployed databases have a schema drift on the ``scheduled_reports`` table:

- **staging DB** (``staging_modulo`` on ``modulo-staging-db``) is stuck at
  alembic revision ``0010_fix_enforce_same_organisation_non_uuid``. Its
  ``scheduled_reports`` table has ``id`` + ``created_at`` but NO ``created_by``
  column. When ``alembic upgrade heads`` runs, migration
  ``0011_database_review_fixes`` (lines 51-57) tries to
  ``CREATE INDEX ix_scheduled_reports_created_by ON scheduled_reports (created_by)
  WHERE created_by IS NOT NULL`` and fails with ``column "created_by" does not
  exist``.
- **prod DB** (``app_modulo`` on ``modulo-app-db``) already has
  ``scheduled_reports.created_by`` (schema is correct), so the fix must NOT emit
  a ``duplicate column`` error there.

The current SQLAlchemy ORM model (``backend/src/modulo/db/models/scheduled_report.py``)
maps ``created_by: Mapped[uuid.UUID | None] = mapped_column(
Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)``.

Alembic has no ``IF NOT EXISTS`` for columns, so this migration uses the standard
idempotent pattern: introspect the live table via ``op.get_bind()`` +
``sa.inspect`` and only emit DDL when the object is genuinely missing. Both the
column and the partial index are guarded this way, so the migration is safe to
run against BOTH schemas (column present and column absent) and to re-run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_add_scheduled_reports_created_by"
down_revision: str | Sequence[str] | None = "0036_break_glass_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind: sa.Connection, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("scheduled_reports")]
    return column_name in columns


def _has_index(bind: sa.Connection, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    indexes = [ix["name"] for ix in inspector.get_indexes("scheduled_reports")]
    return index_name in indexes


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "created_by"):
        op.add_column(
            "scheduled_reports",
            sa.Column(
                "created_by",
                sa.Uuid(),
                sa.ForeignKey("accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if _has_column(bind, "created_by") and not _has_index(bind, "ix_scheduled_reports_created_by"):
        op.create_index(
            op.f("ix_scheduled_reports_created_by"),
            "scheduled_reports",
            ["created_by"],
            unique=False,
            postgresql_where=sa.text("created_by IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "ix_scheduled_reports_created_by"):
        op.drop_index(
            op.f("ix_scheduled_reports_created_by"),
            table_name="scheduled_reports",
        )
    if _has_column(bind, "created_by"):
        op.drop_column("scheduled_reports", "created_by")
