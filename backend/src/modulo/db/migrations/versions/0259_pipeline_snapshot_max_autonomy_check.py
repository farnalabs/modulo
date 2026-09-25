"""Add the autonomy vocabulary CHECKs ``pipeline_snapshots`` is missing (FAR-1223).

Revision ID: 0259_pipeline_snapshot_max_autonomy_check
Revises: 0258_pipeline_accountability_owners
Create Date: 2026-09-24

Migration 0256 added ``max_autonomy_level`` to BOTH ``pipelines`` and
``pipeline_snapshots`` but only guarded the ``pipelines`` column with the
vocabulary CHECK — a snapshot row could store an arbitrary ceiling string that
the run-time resolution path would then treat as unparseable (falling back to
the snapshot default). This closes that gap AND the sibling gap on
``pipeline_snapshots.default_autonomy_level`` (the ``pipelines`` column has
been guarded by ``ck_pipelines_autonomy_level`` since 0003/0110):

    <column> IS NULL OR
    <column> IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')

NULLABILITY — both snapshot columns are nullable (0110 deliberately
``DROP NOT NULL`` on ``pipeline_snapshots.default_autonomy_level``, and no
server default exists), so both CHECKs carry the ``IS NULL OR`` guard. This
DIFFERS from ``ck_pipelines_autonomy_level`` on the live ``pipelines`` column,
which has no NULL arm: the live column always carries a value (server default
``manual_approval``) while a snapshot legitimately freezes "nothing set", and
every existing snapshot INSERT that omits the column stores NULL.

Idempotent/existence-gated (same pattern as 0110/0176/0255/0256): added NOT
VALID first (instant, brief ACCESS EXCLUSIVE) then VALIDATEd in a separate
guarded step (SHARE UPDATE EXCLUSIVE — non-blocking for INSERTs), so
re-running this revision is a no-op. The existence gates are table-qualified
(``conrelid = 'public.pipeline_snapshots'::regclass``) so an identically-named
constraint on a different table cannot make the gate silently skip.
"""

from __future__ import annotations

from alembic import op

revision: str = "0259_pipeline_snapshot_max_autonomy_check"
down_revision: str | None = "0258_pipeline_accountability_owners"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Existence-gated CHECK, added NOT VALID (instant, brief ACCESS EXCLUSIVE) then
# VALIDATEd in a separate guarded step (SHARE UPDATE EXCLUSIVE - non-blocking
# for INSERTs), mirroring the lock-safety pattern introduced in 0176/0255/0256.
# Every gate is table-qualified via conrelid so a same-named constraint on a
# different table cannot satisfy it. Literal DDL (no string formatting): the
# S608 f-string-SQL rule applies to this directory.
_ADD_MAX_CEILING_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipeline_snapshots_max_autonomy_level' "
    "AND conrelid = 'public.pipeline_snapshots'::regclass) "
    "THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT "
    "ck_pipeline_snapshots_max_autonomy_level CHECK ("
    "max_autonomy_level IS NULL OR "
    "max_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_MAX_CEILING_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipeline_snapshots_max_autonomy_level' "
    "AND conrelid = 'public.pipeline_snapshots'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.pipeline_snapshots VALIDATE CONSTRAINT "
    "ck_pipeline_snapshots_max_autonomy_level; END IF; END $$;"
)

_ADD_DEFAULT_LEVEL_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipeline_snapshots_default_autonomy_level' "
    "AND conrelid = 'public.pipeline_snapshots'::regclass) "
    "THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT "
    "ck_pipeline_snapshots_default_autonomy_level CHECK ("
    "default_autonomy_level IS NULL OR "
    "default_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_DEFAULT_LEVEL_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipeline_snapshots_default_autonomy_level' "
    "AND conrelid = 'public.pipeline_snapshots'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.pipeline_snapshots VALIDATE CONSTRAINT "
    "ck_pipeline_snapshots_default_autonomy_level; END IF; END $$;"
)

#: Ordered ``op.execute`` payload: both cheap ACCESS EXCLUSIVE "add NOT VALID"
#: windows first, then both SHARE UPDATE EXCLUSIVE validation scans.
UPGRADE_STATEMENTS: tuple[str, ...] = (
    _ADD_MAX_CEILING_CHECK_NOT_VALID,
    _ADD_DEFAULT_LEVEL_CHECK_NOT_VALID,
    _VALIDATE_MAX_CEILING_CHECK,
    _VALIDATE_DEFAULT_LEVEL_CHECK,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops.
    pass
