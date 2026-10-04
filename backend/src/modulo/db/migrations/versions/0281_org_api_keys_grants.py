"""FAR-1477: ``org_api_keys.grants`` — explicit capability grant-set (ADR 058).

Revision ID: 0281_org_api_keys_grants
Revises: 0280_runs_node_deadline_watchdog_fired_count
Create Date: 2026-10-04

Additive, nullable column (no default, no backfill, no table rewrite)::

    org_api_keys.grants  TEXT NULL

Tri-state semantics (pinned by tests):

* ``NULL``  — legacy role-bundle behaviour. Every pre-existing key keeps it, so
  the migration is byte-identical for existing keys.
* ``''``    — explicit deny-all.
* ``'a b'`` — exact set of space-separated ``PERMISSIONS`` keys.

Deploy ordering (ADR 030): migrate FIRST, then roll code. Old code ignores the
column; new code reads it only behind the ``api_key_grants`` flag (default OFF).
Downgrade drops the column; because only flag-ON mints write non-NULL values,
a rollback cannot silently widen a pre-existing key (they were NULL already).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0281_org_api_keys_grants"
down_revision: str | None = "0280_runs_node_deadline_watchdog_fired_count"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "org_api_keys",
        sa.Column(
            "grants",
            sa.Text(),
            nullable=True,
            comment="Space-separated permission grants; NULL=legacy role bundle, ''=deny-all",
        ),
    )


def downgrade() -> None:
    op.drop_column("org_api_keys", "grants")
