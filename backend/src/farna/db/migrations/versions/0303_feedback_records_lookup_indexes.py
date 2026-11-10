"""Add feedback_records lookup composite indexes.

Revision ID: 0303_feedback_records_lookup_indexes
Revises: 0302_runs_run_grouping
Create Date: 2026-10-12

Two read paths over ``feedback_records`` have no supporting composite index:

1. ``get_feedback_record_for_node`` (``core/feedback_manager/queries.py``) —
   the exact-match hot lookup ``WHERE organisation_id = ? AND run_id = ?
   AND producing_node_id = ?`` on every feedback get-or-create. Only the
   single-column legs (``organisation_id``, ``run_id``) exist;
   ``producing_node_id`` is unindexed by design (0166 defers the FK because
   synthetic node UUIDs would fail it) and Postgres must bitmap-and two
   single-column scans. ``ix_feedback_records_org_run_node`` serves the
   lookup in one btree descent.

2. The feedback list + dashboard trend queries
   (``paginate_feedback_records`` / ``build_org_scoped_conditions`` /
   ``api/routes/dashboard.py``) — ``WHERE organisation_id = ? [AND
   feedback_status = ?]`` with ``ORDER BY created_at DESC`` (or a
   ``created_at`` range + GROUP BY date). No composite leads on
   ``organisation_id`` for this ordering. ``ix_feedback_records_org_status_created``
   on ``(organisation_id, feedback_status, created_at)`` covers the
   status-filtered paginated list (equality, equality, ordered range) and
   the unfiltered list/dashboard sweep via its ``(organisation_id,
   created_at)`` prefix (a btree backward scan serves the DESC ordering, so
   no DESC-keyed index is needed).

Both are additive plain ``CREATE INDEX`` (Alembic runs inside a transaction
so ``CREATE INDEX CONCURRENTLY`` cannot be used — the 0171/0182 precedent),
idempotent (``IF NOT EXISTS``), and mirrored on the ``FeedbackRecord``
model's ``__table_args__`` so ``create_all``-built SQLite schemas and
``compare_metadata`` stay in parity. No partial predicate: every row is a
live candidate for both paths (terminal statuses are still listed).

Deploy-safety: brief table-size-proportional write lock on
``feedback_records`` during upgrade; the table is small (one row per human
rejection), so the lock window is negligible.

Downgrade: drops both indexes.
"""

from __future__ import annotations

from alembic import op

revision: str = "0303_feedback_records_lookup_indexes"
down_revision: str | None = "0302_runs_run_grouping"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "feedback_records"
_ORG_RUN_NODE_INDEX = "ix_feedback_records_org_run_node"
_ORG_STATUS_CREATED_INDEX = "ix_feedback_records_org_status_created"


def upgrade() -> None:
    op.create_index(
        _ORG_RUN_NODE_INDEX,
        _TABLE,
        ["organisation_id", "run_id", "producing_node_id"],
        if_not_exists=True,
    )
    op.create_index(
        _ORG_STATUS_CREATED_INDEX,
        _TABLE,
        ["organisation_id", "feedback_status", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_feedback_records_org_status_created;")
    op.execute("DROP INDEX IF EXISTS ix_feedback_records_org_run_node;")
