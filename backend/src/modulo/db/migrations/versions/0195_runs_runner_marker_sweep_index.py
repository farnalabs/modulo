"""Runner dispatch-marker reconciliation sweep partial index (FAR-594 D8, qa F8).

Creates the D8 marker sweep's backing partial index on ``runs``:

    CREATE INDEX IF NOT EXISTS ix_runs_org_runner_marker_sweep
        ON runs (organisation_id)
        WHERE sandbox_dispatch_state IS NOT NULL

The sweep body (``core.runner_capacity.reconcile_runner_dispatch_markers``)
selects ``organisation_id = :org AND sandbox_dispatch_state IS NOT NULL`` (a
tiny slice of the runs table — markers exist only on sandbox-dispatched runs
and are cleared on terminal/stale rows). Without an index every per-org pass
degrades to a full scan of that org's runs; the partial index keeps the
steady-state probe at an index-only walk of the (usually near-empty) marker
set. The batched cursor scan keys on ``id`` ordering, which the index also
serves.

DEPLOY-SAFETY (0193/0128 precedent): plain ``CREATE INDEX`` — NOT ``CREATE
INDEX CONCURRENTLY``. env.py wraps the WHOLE chain in one externally-managed
``engine.begin()`` transaction, under which Alembic's ``autocommit_block()``
escape cannot function, and ``CONCURRENTLY`` cannot run inside a transaction
block at all. Same trade as every other ``runs`` index migration: a blocking
build with a brief write lock on ``runs`` — expect a table-size-proportional
upgrade; deploy outside traffic peaks, and schedule a post-deploy
``CREATE INDEX CONCURRENTLY`` + assert-only follow-up if the build proves too
long for the window. ``IF NOT EXISTS`` keeps the migration idempotent and
re-runnable (release.sh retries migrations 3x).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0195_runs_runner_marker_sweep_index"
down_revision: str | None = "0194_uuid_pk_server_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "runs"
_INDEX_NAME = "ix_runs_org_runner_marker_sweep"
_PREDICATE = "sandbox_dispatch_state IS NOT NULL"

_CREATE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} "  # nosec B608 - interpolates module constants only, never caller data
    f"ON {_TABLE} (organisation_id) WHERE {_PREDICATE}"
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
