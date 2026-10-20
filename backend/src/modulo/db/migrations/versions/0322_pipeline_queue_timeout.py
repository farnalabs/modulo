"""Add ``pipelines.queue_timeout_seconds`` (FAR-1883).

Revision ID: 0322_pipeline_queue_timeout
Revises: 0320_evidence_redaction_rules
Create Date: 2026-10-18

The instance-wide 120-minute capacity-queue TTL (``capacity_timeout``) is
removed: a capacity-queued run now waits INDEFINITELY by default, because the
old terminal-fail silently lost work that could simply have waited. Operators
can opt a pipeline into a bounded wait with this nullable column:

* ``NULL`` (the default, and every pre-existing row) = NEVER expire. The
  queue-state alerting (FAR-1343/FAR-1344) is the escalation path.
* a positive number of seconds = a capacity-queued run (``status='pending'``
  with a capacity marker - ``org_capacity_limited`` / ``pipeline_capacity``)
  whose ``created_at`` ("time queued") is older than the window is ENDED as a
  SUPERSESSION: ``status='rejected'``, ``error_code='queue.expired'``. It is
  NOT a failure and pages nobody. Never-dispatched runs keep their own 300s
  path; enqueue-failed runs keep their own TTL - one TTL per state class.

A cron pipeline should set this to its period (1h cron -> 3600) so a fire that
can no longer be useful self-drops; webhook/manual/MCP pipelines leave NULL.

Envelope 1..604800 seconds (7 days), mirrored by the ORM CHECK and the API.
Dialect-guarded (0265/0316 pattern): Postgres uses existence-gated raw DDL and
adds the CHECK ``NOT VALID`` then ``VALIDATE CONSTRAINT``; SQLite uses batch
mode (it has no ``ADD COLUMN IF NOT EXISTS`` / ``ADD CONSTRAINT``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0322_pipeline_queue_timeout"
down_revision: str | None = "0321_organisation_cost_state"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "pipelines"
_COLUMN = "queue_timeout_seconds"
_CHECK_NAME = "ck_pipelines_queue_timeout"

#: Literal DDL only (no f-string/format-built SQL - migration-fstring-sql
#: semgrep rule). Existence-gated + table-qualified so a same-named CHECK on
#: another table cannot make the gate silently skip.
_ADD_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipelines_queue_timeout' "
    "AND conrelid = 'public.pipelines'::regclass) "
    "THEN ALTER TABLE public.pipelines ADD CONSTRAINT "
    "ck_pipelines_queue_timeout CHECK ("
    "queue_timeout_seconds IS NULL "
    "OR (queue_timeout_seconds >= 1 AND queue_timeout_seconds <= 604800)"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipelines_queue_timeout' "
    "AND conrelid = 'public.pipelines'::regclass "
    "AND NOT convalidated) "
    "THEN ALTER TABLE public.pipelines "
    "VALIDATE CONSTRAINT ck_pipelines_queue_timeout; END IF; END $$;"
)
_DROP_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipelines_queue_timeout' "
    "AND conrelid = 'public.pipelines'::regclass) "
    "THEN ALTER TABLE public.pipelines "
    "DROP CONSTRAINT ck_pipelines_queue_timeout; END IF; END $$;"
)

#: SQLite batch-mode predicate - must match the model CheckConstraint EXACTLY
#: so metadata.create_all() / SQLite-mirror CTAS parity holds.
_CHECK_PREDICATE = "queue_timeout_seconds IS NULL OR (queue_timeout_seconds >= 1 AND queue_timeout_seconds <= 604800)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # 1. Column. Nullable with NO server default, so every pre-existing row
    #    means "never expire" (the new default) without a backfill.
    if _is_postgres():
        op.execute(sa.text("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS queue_timeout_seconds INTEGER"))
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=True))

    # 2. CHECK - Postgres: NOT VALID + VALIDATE (0274 pattern); SQLite: batch.
    if _is_postgres():
        op.execute(sa.text(_ADD_CHECK_NOT_VALID))
        op.execute(sa.text(_VALIDATE_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_check_constraint(_CHECK_NAME, _CHECK_PREDICATE)


def downgrade() -> None:
    if _is_postgres():
        op.execute(sa.text(_DROP_CHECK))
        op.execute(sa.text("ALTER TABLE pipelines DROP COLUMN IF EXISTS queue_timeout_seconds"))
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_constraint(_CHECK_NAME, type_="check")
            batch_op.drop_column(_COLUMN)
