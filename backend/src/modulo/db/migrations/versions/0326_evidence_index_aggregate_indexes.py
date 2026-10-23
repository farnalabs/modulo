"""FAR-2055: index the compliance evidence index's aggregate scans.

The compliance evidence index (``compliance/evidence_index.py``) runs four
org-wide aggregates per request. Before this migration three of them were not
served by a composite index, so a mature instance paid an O(org rows) heap scan
on every admin-triggered call:

1. **HITL decisions** - ``WHERE organisation_id = ? AND decision IS NOT NULL``
   ``ORDER BY decision_at DESC NULLS LAST LIMIT 10000`` (plus a ``decision_at``
   range when the request is scoped to an audit period). The only composite on
   the table was ``ix_hitl_claims_sweep_actor_window``
   ``(organisation_id, decided_by, decision_at)``, which leads on a column this
   query never filters. New PARTIAL index
   ``ix_hitl_claims_org_decision_at`` ``(organisation_id, decision_at) WHERE
   decision IS NOT NULL``: the predicate matches the query's filter verbatim,
   and undecided gates (the growing majority) never enter the index.

2. **Run snapshots** - ``count(DISTINCT snapshot_id)`` over
   ``runs WHERE organisation_id = ?`` (plus the period on ``created_at``).
   The single-column ``ix_runs_organisation_id`` / ``ix_runs_snapshot_id``
   indexes cannot answer a DISTINCT without a heap fetch per run. New composite
   ``ix_runs_org_snapshot_id`` ``(organisation_id, snapshot_id)`` makes it an
   index-only scan. ``snapshot_id`` is immutable on a run, so the write cost is
   one narrow entry per run creation, not one per status transition.

3. **Eval results** - ``count(DISTINCT eval_id)`` over
   ``eval_results WHERE organisation_id = ?``. The pre-existing
   ``ix_eval_results_org_evaluated_at`` (0218) serves the count/sum/max legs and
   the period range; ``ix_eval_results_eval_id`` is single-column and cannot
   serve the org-bounded DISTINCT. New composite
   ``ix_eval_results_org_eval_id`` ``(organisation_id, eval_id)``.

The audit-chain aggregate is deliberately NOT indexed here: migration 0155
already shipped ``ix_audit_events_org_created_at``, which serves its
count/min/max and the period range. No speculative index is added.

Each index is declared on the SQLAlchemy model in the SAME change (the
``database.md`` model/migration parity rule); SQLite ``create_all`` schemas
get all three (including the partial predicate via ``sqlite_where``) from the
models.

Plain, idempotent ``CREATE INDEX IF NOT EXISTS`` via ``op.create_index`` and
Postgres-only, because ``CREATE INDEX CONCURRENTLY`` cannot run inside Alembic's
transaction block (the 0171/0182/0309 precedent). Deploy-safety: brief
table-size-proportional write lock per index; ``runs`` is the large table, so
schedule outside peaks.

Revision ID: 0326_evidence_index_aggregate_indexes
Revises: 0325_run_seam_coverage
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0326_evidence_index_aggregate_indexes"
down_revision = "0325_run_seam_coverage"
branch_labels = None
depends_on = None

_HITL_TABLE = "hitl_claims"
_HITL_INDEX = "ix_hitl_claims_org_decision_at"

_RUNS_TABLE = "runs"
_RUNS_INDEX = "ix_runs_org_snapshot_id"

_EVAL_RESULTS_TABLE = "eval_results"
_EVAL_RESULTS_INDEX = "ix_eval_results_org_eval_id"

# The evidence index's HITL scan filters decided rows only. Matched verbatim by
# the partial index predicate so the planner can prove the query implies it.
_DECIDED_WHERE = sa.text("decision IS NOT NULL")


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        # SQLite/MariaDB parity comes from the models' __table_args__ via
        # create_all (the 0309 precedent).
        return
    op.create_index(
        _HITL_INDEX,
        _HITL_TABLE,
        ["organisation_id", "decision_at"],
        postgresql_where=_DECIDED_WHERE,
        sqlite_where=_DECIDED_WHERE,
        if_not_exists=True,
    )
    op.create_index(
        _RUNS_INDEX,
        _RUNS_TABLE,
        ["organisation_id", "snapshot_id"],
        if_not_exists=True,
    )
    op.create_index(
        _EVAL_RESULTS_INDEX,
        _EVAL_RESULTS_TABLE,
        ["organisation_id", "eval_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.drop_index(_EVAL_RESULTS_INDEX, table_name=_EVAL_RESULTS_TABLE, if_exists=True)
    op.drop_index(_RUNS_INDEX, table_name=_RUNS_TABLE, if_exists=True)
    op.drop_index(_HITL_INDEX, table_name=_HITL_TABLE, if_exists=True)
