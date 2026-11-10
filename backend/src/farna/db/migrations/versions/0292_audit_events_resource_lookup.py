"""Add resource-lookup index for audit_events + chain-head column default.

Schema legs:

1. ``ix_audit_events_org_resource`` — composite index on
   ``(organisation_id, resource_type, resource_id)``. Every list/export
   path filters by ``resource_type`` (``_apply_filters`` in
   ``core/audit_logger``), and point lookups by ``(resource_type,
   resource_id)`` serve the run-workspace probe (``api/routes/runs.py``)
   and the MCP audit resource lookups. The existing FAR-748 composite
   covers ``(organisation_id, event_type, account_id, created_at)`` and
   cannot serve a leading-``resource_type`` shape, so without this index
   those queries fall back to a full org scan past the unrelated
   single-column indexes. It is a narrower, org-led twin of
   ``0155_add_hot_query_indexes``' ``ix_audit_events_resource`` on
   ``(resource_type, resource_id)``; the org-led shape is required so the
   RLS org filter is the index prefix, at the cost of one more index to
   maintain on the write-heavy ``audit_events`` table.

2. ``audit_chain_heads.event_count`` server default ``0`` (Postgres
   only; SQLite/ORM-created test schemas get it from the model's
   ``server_default`` via ``create_all``, the 0246 precedent). The column
   is ``NOT NULL`` with only a Python-side ``default=0``, so a raw-SQL
   INSERT omitting ``event_count`` fails with a NOT NULL violation.

The non-negative counter guard on ``audit_chain_heads.event_count`` is
already owned by migration ``0165_add_check_constraints`` (constraint
``ck_audit_event_event_count``, added ``NOT VALID`` then ``VALIDATE``-d and
never dropped since; documented as migration-owned in
``tests/integration/test_initial_migration.py``). This migration therefore
does NOT re-add it: a second ``CHECK (event_count >= 0)`` would enforce the
identical predicate twice on every write to the hot ``audit_chain_heads``
table (the chain head is upserted on every audit event) and the pre-flight
violation scan would be dead code on any chain-migrated DB.

Downgrade: drops the index and the column default.

Revision ID: 0292_audit_events_resource_lookup
Revises: 0291_invitations_lookup_constraints
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0292_audit_events_resource_lookup"
down_revision: str | None = "0291_invitations_lookup_constraints"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEX = "ix_audit_events_org_resource"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "audit_events",
        ["organisation_id", "resource_type", "resource_id"],
    )
    if not _is_postgres():
        return
    op.get_bind().execute(sa.text("ALTER TABLE audit_chain_heads ALTER COLUMN event_count SET DEFAULT 0"))


def downgrade() -> None:
    if _is_postgres():
        op.get_bind().execute(sa.text("ALTER TABLE audit_chain_heads ALTER COLUMN event_count DROP DEFAULT"))
    op.drop_index(_INDEX, table_name="audit_events")
