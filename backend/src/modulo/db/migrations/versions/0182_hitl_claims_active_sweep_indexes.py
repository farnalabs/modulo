"""Add HITL active-claim sweep partial indexes (FAR-604 capacity sweeps).

Revision ID: 0182_hitl_claims_active_sweep_indexes
Revises: 0181_org_api_keys_scope
Create Date: 2026-09-06

Two per-tick system crons scan ``hitl_claims`` for every organisation on
each scheduler pass:

1. ``hitl_manager.overdue_warning`` (``hitl_overdue`` cron) —
   ``_fetch_overdue_entries`` filters ``organisation_id = ? AND decision IS
   NULL AND account_id IS NOT NULL AND claimed_at < ? AND
   overdue_notified_at IS NULL``.
2. ``hitl_manager.expiry_job`` (claim-expiry cron) —
   filters ``organisation_id = ? AND expires_at < ? AND account_id IS NOT
   NULL AND decision IS NULL``.

``hitl_claims`` grows with every human-gated run and accumulates one row per
gate even after it is decided (the row is kept as the audit trail of the
human's verdict), so the undecided subset is a small, stable minority while
the table itself keeps growing. Without a partial index each tick full-scans
every org's claims.

Both sweeps share the predicate ``decision IS NULL AND account_id IS NOT
NULL`` (an *active* claim — claimed but not yet decided). Add two partial
indexes leading on ``organisation_id`` (the equality column, matching the
per-org loop in both jobs) with the sweep's range column as the second key:

- ``ix_hitl_claims_overdue_sweep`` (organisation_id, claimed_at) WHERE
  decision IS NULL AND account_id IS NOT NULL AND overdue_notified_at IS NULL
  — covers the overdue sweep (the ``overdue_notified_at IS NULL`` clause is
  the strongest elimination: once alerted a claim drops out of the sweep
  permanently, so this index stays tiny).
- ``ix_hitl_claims_expiry_sweep`` (organisation_id, expires_at) WHERE
  decision IS NULL AND account_id IS NOT NULL — covers the expiry sweep.

Both are additive, online-safe (plain ``CREATE INDEX``; Alembic runs inside a
transaction so ``CREATE INDEX CONCURRENTLY`` cannot be used — consistent with
0171) and idempotent (``IF NOT EXISTS``), matching the partial-index pattern
established in 0168/0171. No model change needed; the model already declares
the columns.

Deploy-safety: expect a table-size-proportional write lock on ``hitl_claims``
during upgrade; schedule outside traffic peaks. The table is small relative to
``runs`` so the lock window is brief.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0182_hitl_claims_active_sweep_indexes"
down_revision: str | None = "0181_org_api_keys_scope"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "hitl_claims"
_OVERDUE_NAME = "ix_hitl_claims_overdue_sweep"
_EXPIRY_NAME = "ix_hitl_claims_expiry_sweep"

# Shared by both active-claim sweeps: a claim is "active" once it has been
# claimed (account_id IS NOT NULL) but not yet decided (decision IS NULL).
_ACTIVE_WHERE = sa.text("decision IS NULL AND account_id IS NOT NULL")
# The overdue sweep additionally narrows to claims that have not yet been
# alerted — once alerted, a claim leaves the sweep permanently.
_OVERDUE_WHERE = sa.text("decision IS NULL AND account_id IS NOT NULL AND overdue_notified_at IS NULL")


def upgrade() -> None:
    op.create_index(
        _OVERDUE_NAME,
        _TABLE,
        ["organisation_id", "claimed_at"],
        postgresql_where=_OVERDUE_WHERE,
        if_not_exists=True,
    )
    op.create_index(
        _EXPIRY_NAME,
        _TABLE,
        ["organisation_id", "expires_at"],
        postgresql_where=_ACTIVE_WHERE,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(_OVERDUE_NAME, table_name=_TABLE, if_exists=True)
    op.drop_index(_EXPIRY_NAME, table_name=_TABLE, if_exists=True)
