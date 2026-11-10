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

NULLABILITY: both snapshot columns are nullable (0110 deliberately
``DROP NOT NULL`` on ``pipeline_snapshots.default_autonomy_level``, and no
server default exists), so both CHECKs carry an explicit ``IS NULL OR`` arm
and every snapshot INSERT that omits the column stores NULL. The live
``ck_pipelines_autonomy_level`` has no explicit NULL arm, but that does NOT
make the two forms behave differently: ``pipelines.default_autonomy_level`` is
nullable too (``Mapped[str | None]``; 0110 ``DROP NOT NULL``s it, and
``PATCH {"default_autonomy_level": null}`` stores NULL despite the server
default of ``manual_approval``), and under Postgres three-valued logic a NULL
operand makes the CHECK predicate NULL, which SATISFIES the constraint. NULL
therefore passes BOTH forms - the two constraints are NULL-equivalent. The
snapshot form states the NULL arm explicitly only because that column is
nullable by design; the constraint text itself is correct as written and is
NOT changed here.

Idempotent/existence-gated (same pattern as 0110/0176/0255/0256): added NOT
VALID first, then VALIDATEd in a separate guarded step, so re-running this
revision is a no-op. The existence gates are table-qualified
(``conrelid = 'public.pipeline_snapshots'::regclass``) so an identically-named
constraint on a different table cannot make the gate silently skip.

LOCKING - the add-then-validate split does NOT make this migration
online-safe under this repo's upgrade path. ``ADD CONSTRAINT ... NOT VALID``
takes ACCESS EXCLUSIVE; ``VALIDATE CONSTRAINT`` takes SHARE UPDATE EXCLUSIVE
(which on its own does not block INSERTs). But ``env.py``'s
``run_migrations_online`` wraps the WHOLE ``alembic upgrade heads`` in one
``engine.begin()`` and never sets ``transaction_per_migration``, so the ADD's
ACCESS EXCLUSIVE stays held - by that same still-open transaction - across
BOTH ``VALIDATE`` full-table scans and until every migration in the run has
committed. Concurrent DML on ``pipeline_snapshots`` therefore blocks for the
duration of the whole upgrade run, not merely for the instant the ADD takes
its lock. The split only buys non-blocking DML if the ADD's own transaction
commits first (per-migration transactions, or the two statements executed as
separate alembic invocations).
"""

from __future__ import annotations

from alembic import op

revision: str = "0259_pipeline_snapshot_max_autonomy_check"
down_revision: str | None = "0258_pipeline_accountability_owners"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Existence-gated CHECK, added NOT VALID (ACCESS EXCLUSIVE - held until the
# single upgrade transaction commits; see the docstring's LOCKING note) then
# VALIDATEd in a separate guarded step (SHARE UPDATE EXCLUSIVE - only
# non-blocking for INSERTs once the ADD has committed), mirroring the
# lock-safety pattern introduced in 0176/0255/0256.
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

#: Ordered ``op.execute`` payload: both ACCESS EXCLUSIVE "add NOT VALID"
#: windows first (their locks are held until the upgrade transaction commits),
#: then both SHARE UPDATE EXCLUSIVE validation scans.
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
