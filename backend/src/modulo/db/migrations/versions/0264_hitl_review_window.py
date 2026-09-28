"""Configurable HITL review window: terminalize_at + per-pipeline override (FAR-1257).

Revision ID: 0264_hitl_review_window
Revises: 0263_evidence_layer
Create Date: 2026-09-27

Two additive, nullable columns:

1. ``hitl_claims.terminalize_at`` — an ABSOLUTE deadline stamped once at fire
   time (``HITLManager.create_gate``) by resolving the review-window chain
   (pipeline override > org default > instance/env default). The FAR-648
   terminalizer's deadline predicate collapses to ``terminalize_at < now()``
   for stamped rows; NULL keeps the legacy ``expires_at + grace`` arithmetic
   as the fallback for every row that fired before this column existed.

   Deliberately NOT ``expires_at``: that column is the claim TTL, reset by the
   claim-expiry job on every claim/reset — overloading it would make the
   terminalization deadline move whenever a claim is re-armed.

2. ``pipelines.hitl_review_window_seconds`` — the per-pipeline override,
   mirroring ``node_timeout_seconds``'s shape (plain Integer on ``pipelines``).
   NULL means "no pipeline override — inherit the org default, then the
   instance default". Guarded by ``ck_pipelines_hitl_review_window`` (NULL or
   60..604800, the same envelope Pydantic enforces on the API surfaces).

Index: ``ix_hitl_claims_terminalize_sweep`` — partial on ``terminalize_at``
restricted to the terminalizer's population (open + unclaimed claims), so a
deadline-range scan of that population is index-backed rather than a sequential
scan of a table that grows one row per fired gate forever. The correlated
``EXISTS`` in the sweep itself still drives off ``ix_hitl_claims_run_id``.

Postgres and SQLite are both handled: the CHECK is added NOT VALID then
VALIDATEd on Postgres (lock-safety pattern from 0176/0255/0256); SQLite has no
ALTER TABLE ADD CONSTRAINT, so it goes through batch mode (the 0262 pattern).
Columns are existence-gated (``IF NOT EXISTS``) so re-running is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0264_hitl_review_window"
down_revision: str | None = "0263_evidence_layer"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_CLAIMS_TABLE = "hitl_claims"
_PIPELINES_TABLE = "pipelines"
_TERMINALIZE_INDEX = "ix_hitl_claims_terminalize_sweep"
_WINDOW_CHECK = "ck_pipelines_hitl_review_window"

# The shipped envelope (ge=60 le=604800 = 1 min .. 7 days), mirrored at every
# layer: this CHECK, the API Pydantic fields, and resolve_hitl_review_window_seconds.
_WINDOW_MIN = 60
_WINDOW_MAX = 604800


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


# Literal DDL (no string formatting): the S608 f-string-SQL rule applies to
# this directory. Existence-gated + NOT VALID then VALIDATE, mirroring 0256.
_ADD_WINDOW_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipelines_hitl_review_window' "
    "AND conrelid = 'public.pipelines'::regclass) "
    "THEN ALTER TABLE public.pipelines ADD CONSTRAINT ck_pipelines_hitl_review_window CHECK ("
    "hitl_review_window_seconds IS NULL OR "
    "hitl_review_window_seconds BETWEEN 60 AND 604800"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_WINDOW_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipelines_hitl_review_window' "
    "AND conrelid = 'public.pipelines'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.pipelines VALIDATE CONSTRAINT "
    "ck_pipelines_hitl_review_window; END IF; END $$;"
)
_DROP_WINDOW_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipelines_hitl_review_window' "
    "AND conrelid = 'public.pipelines'::regclass) "
    "THEN ALTER TABLE public.pipelines DROP CONSTRAINT "
    "ck_pipelines_hitl_review_window; END IF; END $$;"
)

# The terminalizer's population: an open (undecided) claim nobody has claimed.
# Matches the sweep's per-claim gate exactly so the partial index only ever
# holds live candidates, never decided/claimed history rows.
_TERMINALIZE_SWEEP_WHERE = sa.text("decision IS NULL AND account_id IS NULL")


def upgrade() -> None:
    # --- Columns (existence-gated, both dialects) ---
    if _is_postgres():
        op.execute("ALTER TABLE hitl_claims ADD COLUMN IF NOT EXISTS terminalize_at timestamp with time zone;")
        op.execute('ALTER TABLE public."pipelines" ADD COLUMN IF NOT EXISTS "hitl_review_window_seconds" integer;')
    elif _is_sqlite():
        # SQLite has no ADD COLUMN IF NOT EXISTS — batch mode recreates the
        # table from the reflected schema (the 0262 pattern).
        with op.batch_alter_table(_CLAIMS_TABLE) as batch_op:
            batch_op.add_column(sa.Column("terminalize_at", sa.DateTime(timezone=True), nullable=True))
        with op.batch_alter_table(_PIPELINES_TABLE) as batch_op:
            batch_op.add_column(sa.Column("hitl_review_window_seconds", sa.Integer(), nullable=True))

    # --- CHECK constraint on the pipeline override ---
    if _is_postgres():
        op.execute(_ADD_WINDOW_CHECK_NOT_VALID)
        op.execute(_VALIDATE_WINDOW_CHECK)
    elif _is_sqlite():
        with op.batch_alter_table(_PIPELINES_TABLE) as batch_op:
            batch_op.create_check_constraint(
                _WINDOW_CHECK,
                f"hitl_review_window_seconds IS NULL OR "
                f"hitl_review_window_seconds BETWEEN {_WINDOW_MIN} AND {_WINDOW_MAX}",
            )

    # --- Sweep index ---
    op.create_index(
        _TERMINALIZE_INDEX,
        _CLAIMS_TABLE,
        ["terminalize_at"],
        postgresql_where=_TERMINALIZE_SWEEP_WHERE,
        sqlite_where=_TERMINALIZE_SWEEP_WHERE,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(_TERMINALIZE_INDEX, table_name=_CLAIMS_TABLE, if_exists=True)

    if _is_postgres():
        op.execute(_DROP_WINDOW_CHECK)
        op.execute('ALTER TABLE public."pipelines" DROP COLUMN IF EXISTS "hitl_review_window_seconds";')
        op.execute("ALTER TABLE hitl_claims DROP COLUMN IF EXISTS terminalize_at;")
    elif _is_sqlite():
        with op.batch_alter_table(_PIPELINES_TABLE) as batch_op:
            batch_op.drop_constraint(_WINDOW_CHECK, type_="check")
            batch_op.drop_column("hitl_review_window_seconds")
        with op.batch_alter_table(_CLAIMS_TABLE) as batch_op:
            batch_op.drop_column("terminalize_at")
