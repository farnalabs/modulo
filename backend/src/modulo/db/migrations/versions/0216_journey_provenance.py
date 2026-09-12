"""Add ``journeys.provenance``/``first_seen_source`` + canon ``reported`` → ``agent`` (FAR-794 slice 1).

Revision ID: 0216_journey_provenance
Revises: 0215_drop_runs_blob_columns
Create Date: 2026-09-12

Schema legs (Postgres only; SQLite/ORM-created test schemas get the columns
and the index from the ``Journey`` model's ``create_all``):

1. ``journeys.provenance`` — ``varchar(30) NOT NULL DEFAULT 'derived'``.
   Added WITH the default; on Postgres 11+ the ADD COLUMN with constant
   default is a metadata-only change (no table rewrite).
   ``latest_provenance`` keeps its existing meaning untouched (compare-and-set
   terminal evidence, FAR-143).
2. ``journeys.first_seen_source`` — nullable ``varchar(30)``; holds the mapped
   first-observed source ONLY when it was recoverable from
   ``latest_provenance``, NULL for legacy rows whose value is unrecoverable
   (NULL / unrecognised legacy value).
3. ``ix_journeys_org_provenance`` — btree ``(organisation_id, provenance)``
   for the org growth / provenance-distribution queries.
   ``runs.work_item_refs`` containment (``@> '[{"source": ...}]'``) is ALREADY
   served by the partial GIN index ``ix_runs_work_item_refs_gin``
   (``WHERE jsonb_array_length(work_item_refs) > 0``, added in 0110) — no new
   runs index is added here; one would just duplicate it.

Data legs (batched + resumable — 0147/FAR-403 pattern; single-statement
full-table rewrites of rows in the wide table are forbidden by the 0129
NUL-byte JSONB precedent):

4. ``runs.work_item_refs``: every entry with ``source == 'reported'`` is
   rewritten to ``source == 'agent'``. Id-keyed batches of
   :data:`_RUNS_BATCH_SIZE`, pre-filtered by the GIN-indexed jsonb containment
   predicate so only rows that still contain a ``"reported"`` element are
   touched, one autocommit commit per batch on a DEDICATED connection (never
   alembic's transactional bind — committing on it closes
   ``context.begin_transaction()`` and loses the version row, FAR-403).
   Reversibility claim: pre-0216 no code path ever wrote ``source='agent'`` —
   the only ``source`` writers were self-report (forced ``"reported"``) and
   canonicalisation (``"derived"`` default) — so every post-backfill
   ``'agent'`` entry was necessarily ``'reported'`` pre-migration and the
   rewrite is exactly reversible. Migration-window assumption: rows written
   DURING the backfill by the pre-upgrade code carry only ``"reported"``
   (already the backfill target; picked up by a later batch pass) or
   ``"derived"`` (untouched); post-upgrade code writes ``"agent"``, which the
   predicate never matches — both windows are safe and re-running converges
   (idempotent).
5. ``journeys.provenance``/``first_seen_source`` backfill from legacy
   ``latest_provenance`` in batches of :data:`_JOURNEYS_BATCH_SIZE`:
   * ``'reported'`` → ``provenance='agent'``,   ``first_seen_source='agent'``
   * ``'derived'``  → ``provenance='derived'``, ``first_seen_source='derived'``
   * anything else (incl. NULL / unrecognised) → the row keeps the defaulted
     ``provenance='derived'`` and ``first_seen_source`` stays NULL.
   The batching predicate targets only rows that still carry a recoverable
   legacy shape (``first_seen_source IS NULL`` AND ``latest_provenance`` in
   the recognised set), so a completed batch is never re-processed and the
   loop exits after one zero-match pass. Unrecoverable rows need no work:
   their expected backfill result equals the column defaults exactly.

Downgrade: drops the index + columns and rewrites ``runs`` ``agent`` →
``reported`` bounded by a deploy-window cutoff (see ``downgrade``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from modulo.db.migrations._rls_ceremony import is_postgres as _is_postgres

revision: str = "0216_journey_provenance"
down_revision: str | None = "0215_drop_runs_blob_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ``runs`` rows are the wide/hot table — 2000 per batch keeps each rewrite
# short. ``journeys`` rows are narrow, so 5000 per batch.
_RUNS_BATCH_SIZE = 2000
_JOURNEYS_BATCH_SIZE = 5000

# jsonb deep containment: true whenever ANY element of the array is an object
# whose ``source`` is ``'reported'`` (elements may carry extra keys — object
# containment is subset-style). Constant literal, never caller input; inlined
# (not a bind) so asyncpg needs no jsonb codec. This is exactly the predicate
# the partial GIN index from 0110 serves.
_REPORTED_CONTAINMENT = '[{"source": "reported"}]'

# The per-row canon CASE used by the journeys backfill (constant literals;
# migrations never import app constants).
_LEGACY_PROVENANCE_WALL = (
    "CASE j.latest_provenance WHEN 'reported' THEN 'agent' WHEN 'derived' THEN 'derived' ELSE j.provenance END"
)
_LEGACY_FIRST_SEEN_WALL = (
    "CASE j.latest_provenance WHEN 'reported' THEN 'agent' WHEN 'derived' THEN 'derived' ELSE j.first_seen_source END"
)


def _runs_update_sql() -> str:
    """Element-wise ``work_item_refs`` rewrite for one upgrade batch.

    Rewrites ``reported`` → ``agent``. Only runs whose current jsonb value
    still contains a ``reported``-sourced element are touched (self-limiting ⇒
    idempotent); multi-element arrays keep their layout: non-matching elements
    are preserved verbatim via ``jsonb_set``/``jsonb_agg`` with ORDINALITY so
    element order is stable. (The downgrade's reverse ``agent`` → ``reported``
    rewrite is an inline statement in ``downgrade`` bounded by the deploy
    cutoff — see there.)
    """
    return (
        "UPDATE public.runs AS r "  # noqa: S608  # nosec B608 - module constants only, never caller data
        "SET work_item_refs = ("
        "  SELECT COALESCE(jsonb_agg("
        "    CASE WHEN elem->>'source' = 'reported' "
        "         THEN jsonb_set(elem, ARRAY['source'], '\"agent\"'::jsonb) ELSE elem END "
        "    ORDER BY ord"
        "  ), '[]'::jsonb) "
        "  FROM jsonb_array_elements(r.work_item_refs) WITH ORDINALITY AS e(elem, ord)"
        ") "
        f"WHERE r.work_item_refs @> '{_REPORTED_CONTAINMENT}'::jsonb "
        "  AND r.id = ANY(STRING_TO_ARRAY(:ids, ',')::uuid[])"
    )


def _runs_backfill(bind: sa.Connection) -> int:
    """Rewrite ``reported`` → ``agent`` in ``runs.work_item_refs``, batched.

    Returns the number of batches applied (observability only). Batches are
    id-keyed, each statement self-limits to rows still holding a ``reported``
    element, and the trailing zero-match pass terminates the loop. Runs on
    ``op.get_bind()`` INSIDE the migration transaction (0147 precedent): a
    side autocommit connection here could NOT see tables/columns created
    earlier in the same upgrade transaction (proven by the fresh-DB test
    failure that motivated this comment).
    """
    select_sql = text(
        f"SELECT id FROM public.runs WHERE work_item_refs @> '{_REPORTED_CONTAINMENT}'::jsonb ORDER BY id LIMIT :n",  # noqa: S608  # nosec B608 - module constants only
    )
    update_sql = text(_runs_update_sql())
    batches = 0
    while True:
        # Each pass: page ids, rewrite those rows only, advance. A pass whose
        # SELECT matched none means convergence.
        rows = bind.execute(select_sql, {"n": _RUNS_BATCH_SIZE}).fetchall()
        if not rows:
            break
        ids_csv = ",".join(str(row[0]) for row in rows)
        bind.execute(update_sql, {"ids": ids_csv})
        batches += 1
    return batches


def _journeys_backfill(bind: sa.Connection) -> int:
    """Backfill ``journeys.provenance``/``first_seen_source`` from legacy
    ``latest_provenance``, id-keyed batches. Runs on ``op.get_bind()`` inside
    the migration transaction (see ``_runs_backfill`` for why a side
    autocommit connection is disallowed here)."""
    select_sql = text(
        "SELECT j.id FROM public.journeys AS j "
        "WHERE j.first_seen_source IS NULL "
        "  AND j.latest_provenance IN ('reported', 'derived') "
        "ORDER BY j.id LIMIT :n"
    )
    update_sql = text(
        "UPDATE public.journeys AS j SET "  # noqa: S608  # nosec B608 - module constants only, never caller data
        "  provenance = "
        + str(_LEGACY_PROVENANCE_WALL)
        + ", "
        + "  first_seen_source = "
        + str(_LEGACY_FIRST_SEEN_WALL)
        + " "
        + "WHERE j.first_seen_source IS NULL "
        + "  AND j.latest_provenance IN ('reported', 'derived') "
        + "  AND j.id = ANY(STRING_TO_ARRAY(:ids, ',')::uuid[])"
    )
    batches = 0
    while True:
        rows = bind.execute(select_sql, {"n": _JOURNEYS_BATCH_SIZE}).fetchall()
        if not rows:
            break
        ids_csv = ",".join(str(row[0]) for row in rows)
        bind.execute(update_sql, {"ids": ids_csv})
        batches += 1
    return batches


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        # SQLite (ORM-created unit-test schemas) gets the columns + index from
        # the Journey model; there is no stored production data to backfill.
        return

    # --- 1/2: columns (existence-guarded ⇒ re-runs are no-ops) ---
    have_provenance = bind.execute(
        text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'journeys' AND column_name = 'provenance'"
        )
    ).scalar()
    if not have_provenance:
        bind.execute(
            text("ALTER TABLE public.journeys ADD COLUMN \"provenance\" varchar(30) NOT NULL DEFAULT 'derived'")
        )
    have_first_seen = bind.execute(
        text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'journeys' AND column_name = 'first_seen_source'"
        )
    ).scalar()
    if not have_first_seen:
        bind.execute(text('ALTER TABLE public.journeys ADD COLUMN "first_seen_source" varchar(30)'))

    # --- 3: index (IF NOT EXISTS ⇒ idempotent) ---
    bind.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_journeys_org_provenance "
            "ON public.journeys USING btree (organisation_id, provenance)"
        )
    )

    # --- 4/5: data legs (batched; inside the migration transaction) ---
    _journeys_backfill(bind)
    _runs_backfill(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    # REVERSIBILITY + migration-window assumption (documented, last-deploy
    # guard): pre-0216 no code ever wrote ``agent``, so every ``agent`` value
    # present in ``runs.work_item_refs`` at downgrade time was necessarily
    # ``reported`` pre-upgrade — the reverse rewrite restores them exactly.
    # VALID ONLY WITHIN the upgrade's deployment window: rows created/updated
    # at or after the cutoff below may carry GENUINE post-upgrade ``agent``
    # sources (new engine code), which must NOT be teleported back to
    # ``reported`` — they are skipped. The cutoff is the upgrade's staging
    # window start (2026-09-12); extending the replay after that means even
    # older rows see the reverse mapping first, which is why the downgrade
    # also does NOT re-derive ``first_seen_source`` (dropping the columns is
    # the lossless step for journeys — ``latest_provenance`` was never
    # modified by the upgrade).
    deploy_cutoff = "2026-09-12T00:00:00+00:00"
    bind.execute(  # nosec B608 - module constants only, never caller data
        text(
            "UPDATE public.runs AS r "
            "SET work_item_refs = x.refs "
            "FROM ("
            "  SELECT r2.id, COALESCE(jsonb_agg("
            "    CASE WHEN elem->>'source' = 'agent' "
            "         THEN jsonb_set(elem, ARRAY['source'], '\"reported\"'::jsonb) ELSE elem END "
            "    ORDER BY ord"
            "  ), '[]'::jsonb) AS refs "
            "  FROM public.runs AS r2, "
            "       jsonb_array_elements(r2.work_item_refs) WITH ORDINALITY AS e(elem, ord) "
            '  WHERE r2.work_item_refs @> \'[{"source": "agent"}]\'::jsonb '
            "    AND r2.updated_at < CAST(:cutoff AS timestamptz) "
            "  GROUP BY r2.id"
            ") AS x WHERE r.id = x.id"
        ),
        {"cutoff": deploy_cutoff},
    )

    op.execute("DROP INDEX IF EXISTS ix_journeys_org_provenance;")
    op.execute('ALTER TABLE public.journeys DROP COLUMN IF EXISTS "first_seen_source";')
    op.execute('ALTER TABLE public.journeys DROP COLUMN IF EXISTS "provenance";')
