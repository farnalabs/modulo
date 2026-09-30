"""DB-enforce ``pipeline_snapshots.max_autonomy_level >= default_autonomy_level`` (FAR-1280).

Revision ID: 0270_pipeline_snapshots_max_autonomy_ge_default
Revises: 0269_webhook_dedup_check_constraints
Create Date: 2026-09-29

.. warning::
   THIS IS A DATA-MUTATING MIGRATION. Statement 2 below UPDATEs existing
   ``pipeline_snapshots`` rows (it LOWERS ``default_autonomy_level`` on every
   row whose ceiling sits below its default). Read the "Behaviour-preserving
   repair" section before deciding whether to run it against a copy of
   production data.

Why: migration 0264 added the composite CHECK ``ceiling >= default`` to
``pipelines`` and repaired the inverted rows there, but ``pipeline_snapshots``
mirrors the VOCABULARY of both autonomy columns (0259) without mirroring the
PAIR invariant. The asymmetry is now the only gap left: a snapshot row could
still store ``default > ceiling``, which is exactly the state 0264 decided the
database itself must never accept. Snapshot rows are normally written from an
already-invariant pipeline (``crud/pipeline_snapshot.py`` copies both columns
off the live row), so the repair is belt-and-braces - but "normally" is not
"enforced", and nothing downstream repairs an inverted snapshot: run-time
resolution clamps it silently, and rolling one back onto ``pipelines`` would be
rejected by 0264's CHECK.

Statement order: ADD (NOT VALID) -> repair UPDATE -> VALIDATE
------------------------------------------------------------
The three statements run as separate statements inside the ONE transaction
this repo's upgrade path gives every migration (see LOCKING below), in this
order for a reason:

* ``ADD CONSTRAINT ... NOT VALID`` FIRST. NOT VALID checks no existing rows, so
  the add succeeds no matter how many inverted rows are already present. What
  taking the add FIRST does buy is the lock: it acquires ACCESS EXCLUSIVE on
  ``pipeline_snapshots`` before any DML runs, and under this single-transaction
  upgrade that lock is held until the whole run commits. A concurrent writer
  therefore cannot commit an inverted pair at ANY point during this migration.
* The repair UPDATE then rewrites every inverted row. Writers are already
  blocked, so the only inverted rows it can see are pre-existing ones - and it
  cannot be raced by a fresh inverted pair landing between it and the add.
* ``VALIDATE CONSTRAINT`` LAST: a full-table scan that must pass. It runs while
  the add's ACCESS EXCLUSIVE is still held, so the scan cannot observe a row
  that violates the constraint either.

Behaviour-preserving repair (statement 2)
-----------------------------------------
Pre-invariant rows come in three shapes (a non-NULL ceiling with
``rank(default) > rank(ceiling)`` is the only invalid one):

* ``ceiling IS NULL`` - always valid: the effective ceiling IS the default, so
  resolution can only LOWER autonomy. Not touched.
* ``default IS NULL`` - the predicate evaluates NULL and never matches: the app
  treats a NULL default as ``manual_approval`` (the lowest rank), which no
  ceiling can sit below. Not touched.
* ``ceiling IS NOT NULL AND rank(default) > rank(ceiling)`` - INVERTED.

The snapshot is what a run actually reads: ``executor`` pins
``snapshot.default_autonomy_level`` as ``_pipeline_default_autonomy`` AND
``snapshot.max_autonomy_level`` as ``_pipeline_max_autonomy``, and
``resolve_autonomy`` computes ``base = min(default, ceiling)`` (see
``core/run_context/autonomy.py``: ``base = _min_level(base_default, ceiling)``).
An inverted snapshot row therefore ALREADY resolves to ``base = ceiling`` - the
default is dead weight that can never be reached. Lowering
``default_autonomy_level`` to the ceiling leaves ``base``, every recommendation
clamp (``min(rec, ceiling)``), and every observable run behaviour EXACTLY as it
was, while satisfying ``default <= ceiling``. The alternative repair (raise the
ceiling to the default) would CHANGE behaviour by re-opening an autonomy level
the snapshot's runs never had.

The UPDATE is idempotent: once no inverted rows exist its WHERE clause matches
nothing, and it can never produce a row the new CHECK rejects (it moves
``default`` DOWN onto ``ceiling``).

The repair rewrites stored snapshot values, so its rowcount is LOGGED (the same
``bind.execute(...).rowcount`` + ``logger.info`` pattern as 0175/0264) - a
deploy can tell from the migration log whether 0 or N rows changed. A
``RAISE NOTICE`` was not used instead: migration 0192 records that NOTICEs do
not surface deterministically through ``env.py``, so the Python-side log is the
reliable channel.

The composite CHECK (statements 1 and 3)
----------------------------------------
``ck_pipeline_snapshots_max_autonomy_ge_default``::

    max_autonomy_level IS NULL OR
    (CASE default_autonomy_level WHEN 'manual_approval' THEN 0
                                 WHEN 'notify_on_complete' THEN 1
                                 WHEN 'fully_autonomous' THEN 2 END) <=
    (CASE max_autonomy_level WHEN 'manual_approval' THEN 0
                             WHEN 'notify_on_complete' THEN 1
                             WHEN 'fully_autonomous' THEN 2 END)

Byte-identical to 0264's ``ck_pipelines_max_autonomy_ge_default`` predicate -
only the table and the constraint name differ - and mirrored byte-for-byte by
``PipelineSnapshot.__table_args__`` so the ORM and the migrated DDL cannot
drift (a unit test cross-checks the two).

Levels are NOT ordered lexicographically ("fully_autonomous" sorts first as a
raw string), so the comparison goes through the CASE rank map, never through
string ordering - the same map ``_LEVEL_RANK`` uses in
``core/run_context/autonomy.py``.

NULL semantics (three-valued logic): when ``max_autonomy_level IS NULL`` the
first arm is TRUE and the row passes - and BOTH snapshot columns are nullable by
design (0110 ``DROP NOT NULL``s ``default_autonomy_level`` and 0256 makes
``max_autonomy_level`` nullable), so the NULL arms are load-bearing, not
accidental. When the ceiling is non-NULL but ``default_autonomy_level`` is NULL
the CASE yields NULL, the comparison yields NULL, and ``NULL OR NULL`` is NULL -
which SATISFIES a CHECK. That matches ``validate_autonomy_ceiling``, which ranks
a NULL/unparseable default as ``manual_approval`` (the lowest rank, never above
any ceiling). The CHECK deliberately does not spell out a ``default IS NULL``
arm for that reason.

Idempotent/existence-gated (same pattern as 0110/0176/0255/0256/0259/0264): the
CHECK is added NOT VALID first, then VALIDATEd in a separate guarded step, so
re-running this revision is a no-op. Both existence gates are table-qualified
(``conrelid = 'public.pipeline_snapshots'::regclass``) so an identically-named
constraint on a different table cannot make a gate silently skip. Literal DDL
(no string formatting): the S608 f-string-SQL rule applies to this directory.

LOCKING - the add-then-validate split does NOT make this migration online-safe
under this repo's upgrade path. ``ADD CONSTRAINT ... NOT VALID`` takes ACCESS
EXCLUSIVE; ``VALIDATE CONSTRAINT`` takes SHARE UPDATE EXCLUSIVE (which on its
own does not block INSERTs). But ``env.py``'s ``run_migrations_online`` wraps
the WHOLE ``alembic upgrade heads`` in one ``engine.begin()`` and never sets
``transaction_per_migration``, so the ADD's ACCESS EXCLUSIVE stays held - by
that same still-open transaction - across BOTH the repair UPDATE's row locks and
the ``VALIDATE`` full-table scan, until every migration in the run has
committed. Concurrent DML on ``pipeline_snapshots`` therefore blocks for the
duration of the whole upgrade run, not merely for the instant the ADD takes its
lock. The split only buys non-blocking DML if the ADD's own transaction commits
first (per-migration transactions, or the statements executed as separate
alembic invocations).

That held lock is also why the statement order above is sound: a repair-first
order would leave the gap between the repair's row locks releasing and the ADD
committing, during which a concurrent writer could commit an inverted pair that
the NOT VALID add would then never check and the later VALIDATE would abort the
whole upgrade over. Taking the ADD first means the blocking window opens BEFORE
any repair work and never closes until the run commits, so there is no gap to
race.
"""

from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

revision: str = "0270_pipeline_snapshots_max_autonomy_ge_default"
down_revision: str | None = "0269_webhook_dedup_check_constraints"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger(f"alembic.{revision}")

# DATA-MUTATING statement 2: lower the default onto the ceiling for every
# INVERTED row (rank(default) > rank(ceiling)). Behaviour-preserving - see the
# docstring. A NULL default ranks NULL, so ``NULL > rank`` never matches and
# those rows are left alone (a NULL default can never violate the invariant).
#
# The CASE rank fragments below are spelled out inline (never interpolated)
# because the S608 f-string-SQL rule applies to this directory - the repair's
# ``>`` and the CHECK's ``<=`` MUST stay the exact converse of each other, so
# the unit test cross-checks both against the same vocabulary.
REPAIR_INVERTED_DEFAULTS = (
    "UPDATE pipeline_snapshots SET default_autonomy_level = max_autonomy_level "
    "WHERE max_autonomy_level IS NOT NULL "
    "AND (CASE default_autonomy_level "
    "WHEN 'manual_approval' THEN 0 "
    "WHEN 'notify_on_complete' THEN 1 "
    "WHEN 'fully_autonomous' THEN 2 END) > "
    "(CASE max_autonomy_level "
    "WHEN 'manual_approval' THEN 0 "
    "WHEN 'notify_on_complete' THEN 1 "
    "WHEN 'fully_autonomous' THEN 2 END)"
)

# Existence-gated CHECK, added NOT VALID (ACCESS EXCLUSIVE - taken FIRST and
# held until the single upgrade transaction commits; see the docstring's
# LOCKING note) then VALIDATEd in a separate guarded step (SHARE UPDATE
# EXCLUSIVE - only non-blocking for INSERTs once the ADD has committed),
# mirroring 0264 exactly on the snapshot table.
# Every gate is table-qualified via conrelid so a same-named constraint on a
# different table cannot satisfy it. Literal DDL (no f-string SQL): the S608
# f-string-SQL rule applies to this directory.
_ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipeline_snapshots_max_autonomy_ge_default' "
    "AND conrelid = 'public.pipeline_snapshots'::regclass) "
    "THEN ALTER TABLE public.pipeline_snapshots ADD CONSTRAINT "
    "ck_pipeline_snapshots_max_autonomy_ge_default CHECK ("
    "max_autonomy_level IS NULL OR "
    "(CASE default_autonomy_level "
    "WHEN 'manual_approval' THEN 0 "
    "WHEN 'notify_on_complete' THEN 1 "
    "WHEN 'fully_autonomous' THEN 2 END) <= "
    "(CASE max_autonomy_level "
    "WHEN 'manual_approval' THEN 0 "
    "WHEN 'notify_on_complete' THEN 1 "
    "WHEN 'fully_autonomous' THEN 2 END)"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CEILING_GE_DEFAULT_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipeline_snapshots_max_autonomy_ge_default' "
    "AND conrelid = 'public.pipeline_snapshots'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.pipeline_snapshots VALIDATE CONSTRAINT "
    "ck_pipeline_snapshots_max_autonomy_ge_default; END IF; END $$;"
)

#: Ordered statement payload: the existence-gated "add NOT VALID" FIRST (so its
#: ACCESS EXCLUSIVE is taken before any DML and held for the whole single
#: upgrade transaction - see LOCKING), then the behaviour-preserving repair,
#: then the SHARE UPDATE EXCLUSIVE validation scan. ``upgrade()`` runs each in
#: order; the repair additionally logs its rowcount.
UPGRADE_STATEMENTS: tuple[str, ...] = (
    _ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID,
    REPAIR_INVERTED_DEFAULTS,
    _VALIDATE_CEILING_GE_DEFAULT_CHECK,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        if statement == REPAIR_INVERTED_DEFAULTS:
            # DATA-MUTATING: report the rowcount so a deploy can tell whether
            # 0 or N stored snapshot values changed (0175's pattern). Run
            # through the bind rather than op.execute because op.execute
            # returns no result - there is no rowcount to log from it.
            result = op.get_bind().execute(text(statement))
            logger.info(
                "0270 repair: %s inverted pipeline_snapshot row(s) rewritten onto their ceiling",
                result.rowcount,
            )
        else:
            op.execute(statement)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops. The
    # repair is not reverted either - it only removed dead weight, and
    # re-raising a default above its ceiling would recreate rows the app
    # layer rejects today.
    pass
