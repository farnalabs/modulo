"""Re-enable autovacuum on ``runs`` - repo backstop for an out-of-band reloption (FAR-1419).

Revision ID: 0276_runs_autovacuum_enabled
Revises: 0275_run_cancel_reason_vocabulary
Create Date: 2026-10-02

Production's ``runs`` table carries the storage reloption
``autovacuum_enabled=false``. It was applied OUT-OF-BAND: a repo-wide search
for ``autovacuum`` across every migration, model, compose file, fly config and
deploy script returns ZERO matches, so nothing in this repository records it -
the reloption exists only as deployed state, invisible to every schema-parity
check we run. Consequence on prod (2026-10-02):

* ``n_live_tup = 10,411`` vs ``n_dead_tup = 361,302`` (~97% dead tuples);
* table size 7.5 GB, ``last_autovacuum = never``;
* the bloat costs ~11.66% disk reads and ~2.8 s health-check latency
  (staging, without the reloption: 474 ms), which makes prod readiness
  intermittently 504.

Autovacuum is globally ON and healthy - it vacuums ``checkpoints`` and every
other table - so ``runs`` is the SOLE exception, and only because of this
unrecorded reloption.

This migration makes the repository the source of truth again::

    ALTER TABLE public."runs" SET (autovacuum_enabled = true);

It is naturally idempotent (re-running rewrites the same reloption value) and
safe on a fresh database (``true`` is exactly the server default the table
already inherits), so no definition/exists gate is needed - the statement is
an effective no-op on every database that is already correct, and repairs the
deployed one that is not. Nothing here opens a side connection, so the
statement stays inside the migration transaction and rehearses cleanly under
``ALEMBIC_REHEARSAL=1``.

Downgrade is a symmetric ``RESET (autovacuum_enabled)`` rather than the
0108+ reconciliation-chain no-op: it drops the reloption entirely so a
downgraded schema matches a fresh pre-0276 database byte-for-byte (table
inherits the server default - which IS enabled), and ``RESET`` of a reloption
that was never set is a no-op, so the downgrade is safe in both directions.
It deliberately does NOT restore the original out-of-band ``false``: that
value was never in the repo, and re-disabling autovacuum is precisely the
defect this migration repairs.
"""

from __future__ import annotations

from alembic import op

revision: str = "0276_runs_autovacuum_enabled"
down_revision: str | None = "0275_run_cancel_reason_vocabulary"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The reloption write. Table is schema-qualified and the identifier is quoted
# to match how the ORM/alembic address ``runs`` elsewhere in this chain.
_ENABLE = 'ALTER TABLE public."runs" SET (autovacuum_enabled = true);'

# Symmetric, and a no-op when the reloption is absent (RESET of an unset
# storage parameter is accepted and simply re-states the default).
_DISABLE = 'ALTER TABLE public."runs" RESET (autovacuum_enabled);'


def upgrade() -> None:
    op.execute(_ENABLE)


def downgrade() -> None:
    op.execute(_DISABLE)
