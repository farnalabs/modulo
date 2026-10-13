"""Add HITL per-actor sweep-detection composite index.

Revision ID: 0305_hitl_claims_sweep_decided_index
Revises: 0304_metrics_staging_recorded_at_index
Create Date: 2026-10-13

``core/hitl_manager/sweep_alarm.py::count_recent_approves`` (FAR-748 durable
detection) aggregates one actor's committed decisions on every sweep pass::

    WHERE organisation_id = ? AND decided_by = ?
      AND decision IN ('approved', 'deliver_manual')
      AND decision_at IS NOT NULL AND decision_at >= :window_start

counting both ``COUNT(*)`` and ``COUNT(DISTINCT pipeline_id)``. The only
supporting index is the single-column ``ix_hitl_claims_decided_by`` (0217):
Postgres must bitmap-and it with the org index and then filter + range-scan
the actor's full decision history. ``hitl_claims`` grows one row per fired
gate forever — decided rows are kept as the verdict audit trail — so that
history scan grows unbounded while the query only ever needs the recent
window of counted decisions.

``ix_hitl_claims_sweep_decided`` on ``(organisation_id, decided_by,
decision_at)`` serves the lookup in one btree descent (equality, equality,
ordered range). The partial predicate restricts the index to the detection
population — decided-by-actor rows with a counted decision and a stamped
``decision_at`` — so rejected rows and legacy NULL-``decided_by`` rows
(which the detector never counts and which are never backfilled) never
enter it and the index stays proportional to recent approvals, not to
table history.

Additive plain ``CREATE INDEX`` (Alembic runs inside a transaction so
``CREATE INDEX CONCURRENTLY`` cannot be used — the 0171/0182 precedent),
idempotent (``IF NOT EXISTS``), portable via ``postgresql_where`` /
``sqlite_where`` (the 0203/0265/0278/0302 pattern), and declared on the
``HitlClaim`` model's ``__table_args__`` in the SAME change so
``create_all``-built SQLite schemas and autogenerate stay in parity (the
0216/0217 + 0303/0304 rule).

Deploy-safety: brief table-size-proportional write lock on ``hitl_claims``
during upgrade; the counted-decision subset indexed is small relative to
``runs`` so the lock window is brief.

Downgrade: drops the index.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0305_hitl_claims_sweep_decided_index"
down_revision: str | None = "0304_metrics_staging_recorded_at_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "hitl_claims"

#: Index name — asserted against the ``HitlClaim`` model declaration by
#: ``tests/unit/db/test_migration_0305_hitl_claims_sweep_decided_index.py``.
INDEX_NAME: str = "ix_hitl_claims_sweep_decided"

# The sweep detector's population: an actor-stamped committed decision with
# a counted verdict and a stamped decision time. Matches
# ``count_recent_approves`` exactly so the partial index only ever holds
# rows the query can return.
_SWEEP_WHERE = sa.text(
    "decided_by IS NOT NULL AND decision IN ('approved', 'deliver_manual') AND decision_at IS NOT NULL"
)


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        _TABLE,
        ["organisation_id", "decided_by", "decision_at"],
        postgresql_where=_SWEEP_WHERE,
        sqlite_where=_SWEEP_WHERE,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=_TABLE, if_exists=True)
