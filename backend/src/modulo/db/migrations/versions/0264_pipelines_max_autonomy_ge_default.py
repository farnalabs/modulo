"""DB-enforce ``pipelines.max_autonomy_level >= default_autonomy_level`` (FAR-1163 follow-up).

Revision ID: 0264_pipelines_max_autonomy_ge_default
Revises: 0263_evidence_layer
Create Date: 2026-09-28

.. warning::
   THIS IS A DATA-MUTATING MIGRATION. Statement 1 below UPDATEs existing
   ``pipelines`` rows (it LOWERS ``default_autonomy_level`` on every row whose
   ceiling sits below its default) before the composite CHECK is added. Read
   the "Behaviour-preserving repair" section before deciding whether to run it
   against a copy of production data.

Why: the invariant ``max_autonomy_level >= default_autonomy_level`` (levels
ordered ``manual_approval`` (0) < ``notify_on_complete`` (1) <
``fully_autonomous`` (2)) was enforced ONLY by the app-layer PATCH lock in
``update_pipeline_endpoint`` (FAR-1163/FAR-1222). Two concurrent PATCHes that
each validated against their own stale unlocked read could still commit an
inverted pair, so the database itself would accept ``ceiling < default``.
Migration 0259 guarded the VOCABULARY of both autonomy columns; it did NOT
guard their relative order. This migration closes that gap at the DB layer so
no code path - PATCH, graph write, declarative apply, MCP tool, direct SQL -
can store an inverted pair again.

Behaviour-preserving repair (statement 1)
-----------------------------------------
Pre-invariant rows come in three shapes (a non-NULL ceiling with
``rank(default) > rank(ceiling)`` is the only invalid one):

* ``ceiling IS NULL`` - always valid: the effective ceiling IS the default, so
  resolution can only LOWER autonomy. Not touched.
* ``default IS NULL`` - the predicate evaluates NULL and never matches: the
  app treats a NULL default as ``manual_approval`` (the lowest rank), which no
  ceiling can sit below. Not touched.
* ``ceiling IS NOT NULL AND rank(default) > rank(ceiling)`` - INVERTED.
  ``resolve_autonomy`` computes ``base = min(default, ceiling)`` (see
  ``core/run_context/autonomy.py``: ``base = _min_level(base_default, ceiling)``),
  so an inverted row ALREADY resolves to ``base = ceiling`` - the default is
  dead weight that can never be reached. Lowering ``default_autonomy_level`` to
  the ceiling therefore leaves ``base``, every recommendation clamp
  (``min(rec, ceiling)``), and every observable run behaviour EXACTLY as it
  was, while satisfying ``default <= ceiling``. The alternative repair (raise
  the ceiling to the default) would CHANGE behaviour by re-opening an
  autonomy level the row's runs never had.

The UPDATE is idempotent: once no inverted rows exist its WHERE clause matches
nothing, and it can never produce a row the new CHECK rejects (it moves
``default`` DOWN onto ``ceiling``).

The composite CHECK (statements 2-3)
------------------------------------
``ck_pipelines_max_autonomy_ge_default``::

    max_autonomy_level IS NULL OR
    (CASE default_autonomy_level WHEN 'manual_approval' THEN 0
                                 WHEN 'notify_on_complete' THEN 1
                                 WHEN 'fully_autonomous' THEN 2 END) <=
    (CASE max_autonomy_level WHEN 'manual_approval' THEN 0
                             WHEN 'notify_on_complete' THEN 1
                             WHEN 'fully_autonomous' THEN 2 END)

Levels are NOT ordered lexicographically ("fully_autonomous" sorts first as a
raw string), so the comparison goes through the CASE rank map, never through
string ordering - the same map ``_LEVEL_RANK`` uses in
``core/run_context/autonomy.py``.

NULL semantics (three-valued logic): when ``max_autonomy_level IS NULL`` the
first arm is TRUE and the row passes. When the ceiling is non-NULL but
``default_autonomy_level`` is NULL the CASE yields NULL, the comparison yields
NULL, and ``NULL OR NULL`` is NULL - which SATISFIES a CHECK. That matches
``validate_autonomy_ceiling``, which ranks a NULL/unparseable default as
``manual_approval`` (the lowest rank, never above any ceiling). The CHECK
deliberately does not spell out a ``default IS NULL`` arm for that reason.

Idempotent/existence-gated (same pattern as 0110/0176/0255/0256/0259): the
CHECK is added NOT VALID first, then VALIDATEd in a separate guarded step, so
re-running this revision is a no-op. Both existence gates are table-qualified
(``conrelid = 'public.pipelines'::regclass``) so an identically-named
constraint on a different table cannot make a gate silently skip. Literal DDL
(no string formatting): the S608 f-string-SQL rule applies to this directory.

LOCKING - the add-then-validate split does NOT make this migration
online-safe under this repo's upgrade path (same corrected reading as 0259).
``ADD CONSTRAINT ... NOT VALID`` takes ACCESS EXCLUSIVE; ``VALIDATE
CONSTRAINT`` takes SHARE UPDATE EXCLUSIVE (which on its own does not block
INSERTs). But ``env.py``'s ``run_migrations_online`` wraps the WHOLE ``alembic
upgrade heads`` in one ``engine.begin()`` and never sets
``transaction_per_migration``, so the ADD's ACCESS EXCLUSIVE stays held - by
that same still-open transaction - across BOTH the repair UPDATE's row locks
and the ``VALIDATE`` full-table scan, until every migration in the run has
committed. Concurrent DML on ``pipelines`` therefore blocks for the duration
of the whole upgrade run, not merely for the instant the ADD takes its lock.
The split only buys non-blocking DML if the ADD's own transaction commits
first (per-migration transactions, or the statements executed as separate
alembic invocations).
"""

from __future__ import annotations

from alembic import op

revision: str = "0264_pipelines_max_autonomy_ge_default"
down_revision: str | None = "0263_evidence_layer"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# DATA-MUTATING statement 1: lower the default onto the ceiling for every
# INVERTED row (rank(default) > rank(ceiling)). Behaviour-preserving - see the
# docstring. A NULL default ranks NULL, so ``NULL > rank`` never matches and
# those rows are left alone (a NULL default can never violate the invariant).
#
# The CASE rank fragments below are spelled out inline (never interpolated)
# because the S608 f-string-SQL rule applies to this directory - the repair's
# ``>`` and the CHECK's ``<=`` MUST stay the exact converse of each other, so
# the unit test cross-checks both against the same vocabulary.
REPAIR_INVERTED_DEFAULTS = (
    "UPDATE pipelines SET default_autonomy_level = max_autonomy_level "
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

# Existence-gated CHECK, added NOT VALID (ACCESS EXCLUSIVE - held until the
# single upgrade transaction commits; see the docstring's LOCKING note) then
# VALIDATEd in a separate guarded step (SHARE UPDATE EXCLUSIVE - only
# non-blocking for INSERTs once the ADD has committed), mirroring the
# lock-safety pattern introduced in 0176/0255/0256/0259.
# Every gate is table-qualified via conrelid so a same-named constraint on a
# different table cannot satisfy it. Literal DDL (no f-string SQL): the S608
# f-string-SQL rule applies to this directory.
_ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_pipelines_max_autonomy_ge_default' "
    "AND conrelid = 'public.pipelines'::regclass) "
    "THEN ALTER TABLE public.pipelines ADD CONSTRAINT "
    "ck_pipelines_max_autonomy_ge_default CHECK ("
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
    "WHERE conname='ck_pipelines_max_autonomy_ge_default' "
    "AND conrelid = 'public.pipelines'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.pipelines VALIDATE CONSTRAINT "
    "ck_pipelines_max_autonomy_ge_default; END IF; END $$;"
)

#: Ordered ``op.execute`` payload: the behaviour-preserving repair FIRST (the
#: CHECK cannot be added while an inverted row exists), then the ACCESS
#: EXCLUSIVE "add NOT VALID" window (its lock is held until the upgrade
#: transaction commits), then the SHARE UPDATE EXCLUSIVE validation scan.
UPGRADE_STATEMENTS: tuple[str, ...] = (
    REPAIR_INVERTED_DEFAULTS,
    _ADD_CEILING_GE_DEFAULT_CHECK_NOT_VALID,
    _VALIDATE_CEILING_GE_DEFAULT_CHECK,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Reconciliation-chain convention (0108+): downgrades are no-ops. The
    # repair is not reverted either - it only removed dead weight, and
    # re-raising a default above its ceiling would recreate rows the app
    # layer rejects today.
    pass
