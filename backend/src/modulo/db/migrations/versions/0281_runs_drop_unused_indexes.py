"""Drop the three genuinely unused indexes on runs (FAR-1443 audit).

Revision ID: 0281_runs_drop_unused_indexes
Revises: 0280_runs_node_deadline_watchdog_fired_count
Create Date: 2026-10-05

The ``runs`` table carries ~30 indexes — every one is written on every
INSERT/UPDATE, vacuumed independently and cached in buffer memory, on the
single highest-churn write path in the product. FAR-1443 audited all 30
against three signals (structural redundancy, 66.7h of production
``pg_stat_user_indexes`` counters, and a code cross-reference of every query
predicate). Exactly three are dropped here; every other zero-scan index
either backs a real (episodically-used) query or is a constraint, and is
kept:

1. ``ix_runs_account_id`` (account_id) — 0 scans AND no query anywhere
   filters ``runs.account_id``: the column is written at create and read for
   display (``api/routes/runs.py`` labels), and the only WHERE against it is
   ``WHERE id = :rid`` (primary key). Declared in the model via
   ``index=True`` (0128's MAJOR-1 fix) — that flag is removed in the same
   change. Trade-off noted: ``runs_account_id_fkey`` (ON DELETE SET NULL)
   falls back to a sequential scan of ``runs`` if an account row is ever
   deleted — rare, and account deletion is not on any routine path — versus
   continuous write cost on every run write.

2. ``ix_runs_dispatcher`` (dispatcher) — 0 scans despite the
   ``dispatcher``-referencing queries having run ~4,000 times in the same
   window (``dispatcher_reconcile`` composes ``reconciler_recovery_predicate``
   — whose branches name ``dispatcher`` — into its per-org row SELECT every
   60s). The planner rejected the index on every execution: no query filters
   ``dispatcher`` as a leading predicate (it is always bundled with the far
   more selective status/heartbeat conditions), and the column's near-binary
   distribution ('saq' vs NULL) makes it a poor leading key regardless.
   Declared in the model via ``index=True`` (0110) — removed in the same
   change.

3. ``ix_runs_org_status_completed`` (organisation_id, status, completed_at)
   — 0 scans, while its leading-column subset ``ix_runs_organisation_status``
   (organisation_id, status) recorded 110,486 scans: every observed
   (org, status) query chose the narrower index. Its only differentiating
   capability is serving ``ORDER BY completed_at`` under an org + status
   predicate, and no production query has that shape — every terminal +
   ``ORDER BY completed_at`` sweep (journey reconcile, classification, the
   evidence probe) runs through the BYPASSRLS ``modulo_system`` role with no
   ``organisation_id`` qualifier at all, so an org-LEADING index is
   structurally unusable for them (they use
   ``ix_runs_unclassified_terminal`` / plain sorts). The creator migration's
   claimed consumer (a ``run_node_outputs`` ORDER BY completed_at sweep,
   0198) no longer exists in the codebase. Declared only in migration 0198 —
   never in the model — so no model change is needed for this one.

The downgrade recreates all three with their original definitions, so the
drop is reversible end to end (asserted against real Postgres — exact
``pg_indexes.indexdef`` equality — by
``tests/integration/test_migration_0281_runs_drop_unused_indexes.py``).
Statements are literal (no f-string interpolation): DROP INDEX IF EXISTS /
CREATE INDEX IF NOT EXISTS keep the revision idempotent under release.sh's
migration retries, and a blocking DROP takes only a metadata lock (never
CONCURRENTLY — env.py wraps each revision in one transaction, the 0155/0197/
0200 precedent).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0281_runs_drop_unused_indexes"
down_revision = "0280_runs_node_deadline_watchdog_fired_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_account_id")
    op.execute("DROP INDEX IF EXISTS ix_runs_dispatcher")
    op.execute("DROP INDEX IF EXISTS ix_runs_org_status_completed")


def downgrade() -> None:
    # Recreate every dropped index with its original definition — a dropped
    # index that cannot be restored is a one-way door. The integration test
    # asserts each restored definition against pg_indexes.indexdef, so a
    # drift between these literals and the originals fails CI.
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_account_id ON runs (account_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_runs_dispatcher ON runs (dispatcher)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_org_status_completed ON runs (organisation_id, status, completed_at)"
    )
