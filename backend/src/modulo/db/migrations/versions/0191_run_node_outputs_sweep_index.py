"""run_node_outputs sweep partial index (FAR-583 qa iteration 2, Major 7).

Creates the catch-up sweep's backing partial index on ``runs``:

    CREATE INDEX IF NOT EXISTS ix_runs_org_completed_at_terminal_sweep
        ON runs (organisation_id, completed_at)
        WHERE status IN (<the 9-state terminal set>)

The sweep body (``crud.run_node_outputs.backfill_run_node_outputs_batch``)
selects ``organisation_id = :org AND status IN (terminal) ORDER BY
completed_at ASC LIMIT :cap`` every dispatcher_reconcile tick. Without an
index the selection degrades to a full scan of every org's terminal runs on
every 60s tick; the partial index keeps the steady-state tick at an
index-only probe (a drained org selects zero rows via the NOT-EXISTS trigger
legs, but the probe itself still walks the terminal set).

DEPLOY-SAFETY (0171/0128 precedent, deviation from the qa fix's original
plan): plain ``CREATE INDEX`` — NOT ``CREATE INDEX CONCURRENTLY``. env.py
wraps the WHOLE chain in one externally-managed ``engine.begin()``
transaction, under which Alembic's ``autocommit_block()`` escape cannot
function (the MigrationContext's ``_transaction`` is untracked for an
external transaction, so the escape asserts), and ``CONCURRENTLY`` cannot
run inside a transaction block at all. This is the same trade every other
``runs`` index migration made (0128/0154/0155/0171): a blocking build with a
brief write lock on ``runs`` — expect a table-size-proportional upgrade;
deploy outside traffic peaks, and schedule a post-deploy
``CREATE INDEX CONCURRENTLY`` + assert-only follow-up if the build proves too
long for the window. ``IF NOT EXISTS`` keeps the migration idempotent and
re-runnable (release.sh retries migrations 3x).

The terminal-status literal is INLINED (migrations never import app
constants) and MUST equal ``sorted(db.models.run.TERMINAL_STATUSES)`` — the
same twin discipline as 0190 (pinned by tests/unit/db/
test_migration_run_node_outputs.py).

REVISION NUMBERING NOTE: this migration consumes revision number 0191 — the
FAR-583 design doc's planned B2b legacy-column-drop migration becomes **0192**
and PR C's pointer migration becomes **0193**.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0191_run_node_outputs_sweep_index"
down_revision: str | None = "0190_run_node_outputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "runs"
_INDEX_NAME = "ix_runs_org_completed_at_terminal_sweep"

# Inline terminal-status literal (migrations cannot import app constants).
# MUST equal sorted(db.models.run.TERMINAL_STATUSES) — pinned by the unit
# test, and MUST match the sweep body's selection
# (crud.run_node_outputs.backfill_run_node_outputs_batch uses
# db.models.run.TERMINAL_STATUSES directly).
_TERMINAL_RUN_STATUSES = (
    "budget_exceeded",
    "cancelled",
    "compensation_failed",
    "complete",
    "cost_ceiling_exceeded",
    "eval_failed",
    "failed",
    "router_no_match",
    "stalled",
)

# Pure-literal predicate twin assembled FROM the tuple above (single source
# of truth — the two cannot drift apart by construction; the unit test still
# asserts every status literal is present in the assembled predicate).
_TERMINAL_PREDICATE_SQL = "status IN (" + ", ".join(f"'{status}'" for status in _TERMINAL_RUN_STATUSES) + ")"

_CREATE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} "  # nosec B608 - interpolates module constants only, never caller data
    f"ON {_TABLE} (organisation_id, completed_at) "
    f"WHERE {_TERMINAL_PREDICATE_SQL}"
)
_DROP_INDEX_SQL = f"DROP INDEX IF EXISTS {_INDEX_NAME}"


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return
    bind.execute(sa.text(_CREATE_INDEX_SQL))


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return
    bind.execute(sa.text(_DROP_INDEX_SQL))
