"""One-off data repair: historical ``run_daily_facts.benign`` NULLs beside a persisted benign classification (FAR-1665).

Revision ID: 0300_benign_facts_repair
Revises: 0299_accounts_service_identity
Create Date: 2026-10-11

FAR-707's reconcile-restamp fix (migration 0296 + the sweep's re-record in
``classify._reconcile_classify_run``) repairs the LIVE path, but historical
fact rows written BEFORE that fix carry ``benign=NULL`` next to a run that a
later sweep classified ``benign``. Nothing automatically repairs them:

* ``backfill_facts`` only INSERTs rows for runs with NO fact (its anti-join),
  so an existing NULL-marker fact suppresses it forever;
* the reconcile sweep skips already-classified runs (``run_classification is
  not None`` -> ``"skipped"``), so the re-record path never fires for them.

Readers treat NULL as "not benign" (fail-open to the historical counting), so
those historical benign outcomes (e.g. Merge Queue "Rebase conflict" runs
matching a per-pipeline benign signature) keep inflating analytics failure
rates.

This repair stamps ``benign = TRUE`` for every fact whose ``run_id`` carries a
persisted ``benign`` run-classification. Idempotent and non-destructive by
construction:

* the WHERE clause matches ONLY ``benign IS NULL`` rows — a fact already
  stamped ``TRUE``, or confidently ``FALSE`` by the live writer, is never
  touched;
* re-running the statement is a no-op (the repaired rows no longer match the
  NULL predicate);
* facts whose source ``runs`` row was already purged (90-day retention) cannot
  be repaired — the classification lives only on the run row — and simply stay
  NULL, which still reads as "not benign" (the historical counting).

Downgrade is a deliberate no-op: a data repair has nothing to revert.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0300_benign_facts_repair"
down_revision: str | None = "0299_accounts_service_identity"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE run_daily_facts
            SET benign = TRUE
            WHERE run_daily_facts.benign IS NULL
              AND run_daily_facts.run_id IN (
                SELECT runs.id
                FROM runs
                WHERE runs.run_classification ->> 'value' = 'benign'
              )
            """
        )
    )


def downgrade() -> None:
    """Data repair — nothing to revert (deliberate no-op)."""
