"""Add decision CHECK + coalesce/sweep lookup indexes for hitl_claims.

Covers ``backend/src/modulo/db/models/hitl_claim.py``. Three gaps, each
with an invariant provable from the write/query paths:

1. ``ck_hitl_claims_decision`` — the ``decision`` column only ever
   receives the three ``_DECISION_*`` constants written by
   ``HITLManager._decide`` (``approve`` / ``approve_with_modification`` /
   ``reject``) and ``gate_coalescing`` supersede (``rejected``):
   ``'approved'``, ``'rejected'``, ``'deliver_manual'``. The ``skip`` /
   ``replay`` / answer actions live in ``decision_payload.action``, never
   in this column. ``_decide`` takes a free-form ``decision: str``, so
   without a DB-level CHECK a bad value would persist unchecked. NULL
   (undecided) passes the CHECK by three-valued logic.

2. ``ix_hitl_claims_coalesce_scan`` — partial composite index on
   ``(pipeline_id, review_id) WHERE decision IS NULL AND
   account_id IS NULL``. The gate-coalescing candidate scan
   (``gate_coalescing.find_coalesce_candidate``) filters exactly
   ``pipeline_id = ? AND organisation_id = ? AND review_id = ?`` plus
   the unclaimed-undecided scope. Neither the single-column pipeline
   index nor the ``(run_id, review_id)`` unique constraint serves the
   ``(pipeline_id, review_id)`` equality pair.

3. ``ix_hitl_claims_sweep_detection`` — partial composite index on
   ``(organisation_id, decided_by, decision_at) WHERE decided_by IS NOT
   NULL AND decision_at IS NOT NULL``. The FAR-611 sweep alarm's
   per-actor window (``sweep_alarm._count_actor_decisions``) filters
   ``organisation_id = ? AND decided_by = ? AND decision IN (...) AND
   decision_at >= ?``; the single-column ``decided_by`` index cannot
   serve the org-led range scan. Both equality terms are the prefix and
   the range column is last, so the window query is index-backed.

Indexes are created with ``IF NOT EXISTS`` (the guarded style used by
0177/0291) so a re-run never fails. The ``public.``-qualified index SQL
and ``op.create_check_constraint`` (bare ``ALTER TABLE ... ADD
CONSTRAINT``, which SQLite cannot execute) run Postgres-only in
practice; SQLite unit-test schemas get the same two partial indexes and
the same CHECK rule from the ``HitlClaim`` model's ``__table_args__``
(``sqlite_where`` predicates + ``CheckConstraint``) via ``create_all``
(the 0246/0291 precedent).

Downgrade: drops both indexes and the constraint.

Revision ID: 0295_hitl_claims_decision_sweep
Revises: 0294_eval_results_org_fk
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0295_hitl_claims_decision_sweep"
down_revision: str | None = "0294_eval_results_org_fk"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_COALESCE_WHERE = "decision IS NULL AND account_id IS NULL"
_SWEEP_WHERE = "decided_by IS NOT NULL AND decision_at IS NOT NULL"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_hitl_claims_coalesce_scan "
            "ON public.hitl_claims (pipeline_id, review_id) "
            f"WHERE {_COALESCE_WHERE}"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_hitl_claims_sweep_detection "
            "ON public.hitl_claims (organisation_id, decided_by, decision_at) "
            f"WHERE {_SWEEP_WHERE}"
        )
    )
    if not _is_postgres():
        return
    op.create_check_constraint(
        "ck_hitl_claims_decision",
        "hitl_claims",
        sa.text("decision IN ('approved', 'rejected', 'deliver_manual')"),
    )


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint("ck_hitl_claims_decision", "hitl_claims", type_="check")
    op.execute(sa.text("DROP INDEX IF EXISTS public.ix_hitl_claims_sweep_detection"))
    op.execute(sa.text("DROP INDEX IF EXISTS public.ix_hitl_claims_coalesce_scan"))
