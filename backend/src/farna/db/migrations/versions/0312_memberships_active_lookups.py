"""Membership active-lookup index + team role server default.

Schema legs (8-lens review of org_membership.py / team_membership.py):

1. ``ix_org_memberships_org_role_active`` — partial composite index on
   ``(organisation_id, role) WHERE deactivated_at IS NULL``. The
   last-admin guard (``crud/last_admin_guard.py::assert_not_last_admin``,
   executed inside every admin demote / remove / deactivate mutation)
   counts ``WHERE organisation_id = ? AND role = 'admin'`` plus the
   shared ``deactivated_at IS NULL`` liveness scope, and the SCIM admin
   scan (``api/routes/scim.py``) filters the same three terms; the only
   pre-existing leg is the single-column
   ``ix_org_memberships_organisation_id`` (0001), so Postgres must scan
   every membership of the org plus a heap filter on role and liveness.
   The composite serves the lookup in one btree descent, and the partial
   predicate keeps tombstoned rows (which only grow) out of the index.

2. ``team_memberships.role`` server default ``'viewer'``. The model
   declared a client-side ``default="viewer"`` while the DB column
   (0002, no default) has none — a raw-SQL INSERT omitting ``role`` hits
   the NOT NULL constraint, and the behaviour diverges from
   ``org_memberships.role`` which has ``server_default="runner"`` on
   both layers. Now aligned at the DB level; the model moves to
   ``server_default="viewer"`` in the same change.

Both legs are additive and idempotent (``IF NOT EXISTS`` index /
re-runnable ``ALTER COLUMN ... SET DEFAULT``). Plain (blocking)
``CREATE INDEX`` — Alembic runs inside a transaction so ``CREATE INDEX
CONCURRENTLY`` cannot be used (the 0171/0182 precedent); membership
tables hold one row per user per org/team, so the lock is trivial.
The DDL is Postgres-flavoured (partial-index WHERE clause); SQLite
unit-test schemas get the same partial index from the ``OrgMembership``
model's ``__table_args__`` (``sqlite_where``) via ``create_all`` (the
0291 precedent), so the SQLite-ported claim holds at the model layer.

Downgrade: drops the index and the column default.

Revision ID: 0312_memberships_active_lookups
Revises: 0311_library_cluster_schema_hygiene
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0312_memberships_active_lookups"
down_revision: str | None = "0311_library_cluster_schema_hygiene"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "org_memberships"
_INDEX_NAME = "ix_org_memberships_org_role_active"

# An org membership is "active" while its per-org deactivation tombstone
# is unset (gh-1794/FAR-533); only those rows can ever match the
# last-admin guard count or the SCIM admin scan.
_ACTIVE_WHERE = sa.text("deactivated_at IS NULL")


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["organisation_id", "role"],
        postgresql_where=_ACTIVE_WHERE,
        if_not_exists=True,
    )
    op.alter_column(
        "team_memberships",
        "role",
        existing_type=sa.String(length=20),
        nullable=False,
        server_default="viewer",
    )


def downgrade() -> None:
    op.alter_column(
        "team_memberships",
        "role",
        existing_type=sa.String(length=20),
        nullable=False,
        server_default=None,
    )
    op.drop_index(_INDEX_NAME, table_name=_TABLE, if_exists=True)
