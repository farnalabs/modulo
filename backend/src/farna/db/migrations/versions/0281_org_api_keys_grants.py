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

Rollback caveat (IMPORTANT): a key minted with a grant-set (non-NULL ``grants``,
including the explicit empty deny-all) is a RESTRICTED key. Code that predates
this change ignores the column, and dropping the column discards the grant-set,
so either way such a key silently becomes a FULL-role key. Rolling back is
therefore unsafe while any grant-bearing key exists. ``downgrade`` REFUSES to run
while any non-NULL ``grants`` row exists; before rolling code back below this
change, turn the ``api_key_grants`` flag off and revoke every grant-bearing key.
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
    bind = op.get_bind()
    remaining = bind.execute(sa.text("SELECT COUNT(*) FROM org_api_keys WHERE grants IS NOT NULL")).scalar()
    if remaining:
        raise RuntimeError(
            f"Refusing to downgrade 0281: {remaining} org_api_keys row(s) carry a grant-set. "
            "Dropping the column would silently widen them to full-role keys. Turn the "
            "api_key_grants flag off and revoke all grant-bearing keys first, then retry."
        )
    op.drop_column("org_api_keys", "grants")
