"""Convert ``runs`` JSON columns to JSONB non-blockingly (dist db-runs-jsonb).

Revision ID: 0129_runs_json_to_jsonb
Revises: 0128_add_fk_lookup_indexes
Create Date: 2026-08-23

Why this migration avoids ``ALTER COLUMN ... TYPE`` blocking rewrites
--------------------------------------------------------------------
The ``runs`` table is the hottest table in the system — the pipeline runner
writes to it continuously. The original migration used a direct
``ALTER TABLE public."runs" ALTER COLUMN "col" TYPE jsonb USING "col"::jsonb``
for each of the seven columns. Every ``ALTER COLUMN ... TYPE`` takes an
ACCESS EXCLUSIVE lock **and** performs a full-table rewrite. On a live,
constantly-written ``runs`` table the lock is never acquired (the writer keeps
owning it), so the migration hangs forever. That hangs the deploy: the worker
fail-closes once it sees the DB is "not at head", and a migrated-out-of-line
idle-in-transaction session has held the ALTER lock for 14+ minutes (requiring
a manual ``pg_terminate_backend``).

The ``json`` -> ``jsonb`` cast is lossless for every existing row (NULL stays
NULL; well-formed ``json`` re-parses identically as ``jsonb``), so the data
can be converted in place using this additive, per-column algorithm:

1. **Add a temp ``jsonb`` column** ``{col}_jsonb`` (fast: brief ACCESS
   EXCLUSIVE, metadata-only — no data rewrite).
2. **Batch backfill** ``{col}_jsonb = {col}::jsonb`` for rows where
   ``{col}_jsonb IS NULL`` in chunks of 1000. Each UPDATE touches row-level
   locks only — never a table-level exclusive lock held across the whole
   dataset, which is what made the direct ALTER un-acquirable.
3. **Swap** by renaming: ``{col}`` -> ``{col}_old``, then ``{col}_jsonb`` ->
   ``{col}``, then drop ``{col}_old``. Each rename/drop is a brief
   metadata-only lock; there is no data rewrite.
4. **Finalize** by mirroring the original nullability/default.

All statements run on **alembic's own connection** inside the migration's
transaction. Postgres DDL is fully transactional, so the temp-column,
backfill and swap steps are atomic together with the version-row bookkeeping.
An earlier iteration of this migration used a *dedicated autocommit
side-connection* for the same steps; that variant is fundamentally incorrect
under this project's env.py, which wraps an entire ``upgrade`` chain in a
single transaction: on a fresh database the target tables exist only inside
that uncommitted transaction, so the side connection cannot see them at all
(``information_schema`` lookups return nothing) and every conversion silently
skipped while alembic still recorded the revisions as applied — leaving plain
``json`` columns at head. It also deadlocked whenever any table the chain had
already locked was touched again from the outside. Running on the migration's
own connection fixes both failure modes.

Trade-off versus true per-batch commits: the batched backfill holds its row
locks for the duration of the migration instead of releasing them between
chunks. Readers are never blocked and writers contend only on rows actively
being updated; crucially, there is still no long-held ACCESS EXCLUSIVE
table-rewrite, which was the actual deploy-killing behaviour being avoided.

Every phase is idempotent (information_schema column-existence checks gate
ADD/RENAME/DROP and ``WHERE {col}_jsonb IS NULL`` gates the backfill), so a
migration that fails midway can simply be re-run once the failure is cleared:
already-converted columns are skipped and only remaining rows are backfilled.

The ORM model keeps these columns mapped as generic ``sqlalchemy.JSON`` (NOT
``JSONB``) for SQLite/MariaDB parity — the same convention the four
pre-existing jsonb columns follow. Migrations run against Postgres only; the
SQLite unit-test backend uses the ORM model, so the type change lives entirely
in the migration, never in the model.

Downgrade reverses each column ``jsonb`` -> ``json`` using the same pattern
(add ``{col}_json``, batch ``::json`` backfill, swap, drop).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Connection, column, select, table, text, update
from sqlalchemy.dialects.postgresql import JSON as PG_JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0129_runs_json_to_jsonb"
down_revision: str | None = "0128_add_fk_lookup_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Runs columns currently typed ``json`` that this migration promotes to ``jsonb``.
_JSON_COLUMNS: tuple[str, ...] = (
    "cost_breakdown",
    "node_token_usage",
    "input_payload",
    "outputs_json",
    "node_telemetry_json",
    "guardrail_summary_json",
    "variant_config_snapshot",
)

_TABLE = "runs"
_SCHEMA = "public"
_BATCH_SIZE = 1000
_OLD_SUFFIX = "_old"


def _column_meta(bind: Connection, column_name: str) -> dict | None:
    """Return ``{is_nullable, column_default}`` for ``public.runs.{col}`` or None.

    Uses ``information_schema.columns`` so the existence check doubles as a
    read of the original column's nullability/default before any rename
    invalidates it. Runs on the migration bind — the same connection/transaction
    as the rest of the migration — so columns created earlier in the same
    upgrade chain are always visible.
    """
    row = bind.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table AND column_name = :col"
        ),
        {"table": _TABLE, "col": column_name},
    ).fetchone()
    if row is None:
        return None
    return {"is_nullable": row[0], "column_default": row[1]}


def _cast_type(cast: str):
    """Return the Postgres SQLAlchemy type for a ``::cast`` target."""
    return JSONB() if cast == "jsonb" else PG_JSON()


def _backfill(bind: Connection, col: str, tmp: str, cast: str) -> None:
    """Backfill ``tmp`` from ``col`` in bounded batches on the migration bind.

    Uses a SQLAlchemy Core ``update`` (not a raw string) so the row predicate is
    parameterised and no f-string SQL reaches the executor. ``{tmp} IS NULL``
    makes it resumable; ``ctid IN (SELECT ... LIMIT 1000)`` caps each UPDATE so
    it never builds one unbounded statement. Loops until a batch touches zero
    rows.
    """
    runs_tbl = table(_TABLE, column("ctid"), column(col), column(tmp), schema=_SCHEMA)
    pending = runs_tbl.c[tmp].is_(None) & runs_tbl.c[col].isnot(None)
    ctid_subq = select(runs_tbl.c.ctid).where(pending).limit(_BATCH_SIZE)
    stmt = (
        update(runs_tbl)
        .where(pending)
        .where(runs_tbl.c.ctid.in_(ctid_subq))
        .values({tmp: runs_tbl.c[col].cast(_cast_type(cast))})
    )
    while True:
        result = bind.execute(stmt)
        if result.rowcount == 0:
            break


def _add_temp_column(bind: Connection, tmp: str, cast: str) -> None:
    """Add ``tmp`` (typed ``cast``) if absent — brief metadata-only lock."""
    if _column_meta(bind, tmp) is not None:
        return
    bind.execute(text(f'ALTER TABLE public."{_TABLE}" ADD COLUMN "{tmp}" {cast}'))


def _swap(bind: Connection, col: str, tmp: str, old: str) -> None:
    """Rename ``col`` -> ``old``, ``tmp`` -> ``col``, drop ``old``.

    Skips entirely if the ``old`` column already exists (re-run safety). Each
    step is a brief metadata-only lock; no data is rewritten.
    """
    if _column_meta(bind, old) is not None:
        return
    bind.execute(text(f'ALTER TABLE public."{_TABLE}" RENAME COLUMN "{col}" TO "{old}"'))
    bind.execute(text(f'ALTER TABLE public."{_TABLE}" RENAME COLUMN "{tmp}" TO "{col}"'))
    bind.execute(text(f'ALTER TABLE public."{_TABLE}" DROP COLUMN "{old}"'))


def _finalize(bind: Connection, col: str, orig: dict) -> None:
    """Mirror the original column's nullability/default onto the swapped column.

    The temp column is added nullable without a default, so after the swap the
    new ``col`` must be re-constrained to match the original. The cast is
    lossless, so the backfilled data is identical and ``SET NOT NULL`` is safe.
    """
    if orig["is_nullable"] == "NO":
        bind.execute(text(f'ALTER TABLE public."{_TABLE}" ALTER COLUMN "{col}" SET NOT NULL'))
    if orig["column_default"] is not None:
        bind.execute(text(f'ALTER TABLE public."{_TABLE}" ALTER COLUMN "{col}" SET DEFAULT {orig["column_default"]}'))


def _convert(bind: Connection, *, tmp_suffix: str, cast: str) -> None:
    """Convert each ``_JSON_COLUMNS`` column via a temp column on ``bind``.

    ``tmp_suffix`` names the temporary column (``jsonb`` upgrading ``json`` ->
    ``jsonb``, ``json`` downgrading ``jsonb`` -> ``json``); the held-back column
    is always ``{col}_old``. Additive and idempotent, so a failed run resumes
    cleanly on re-execution.
    """
    for col in _JSON_COLUMNS:
        orig = _column_meta(bind, col)
        if orig is None:
            # Column absent (non-standard DB) — nothing to convert.
            continue
        tmp = f"{col}_{tmp_suffix}"
        _add_temp_column(bind, tmp, cast)
        _backfill(bind, col, tmp, cast)
        _swap(bind, col, tmp, f"{col}{_OLD_SUFFIX}")
        _finalize(bind, col, orig)


def upgrade() -> None:
    # Postgres-only: plain ``json`` -> ``jsonb`` cast. SQLite uses the ORM model
    # (generic JSON), so skip on non-Postgres dialects. All statements run on
    # alembic's own connection inside the migration transaction: see the module
    # docstring for why a dedicated autocommit side-connection was rejected.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _convert(bind, tmp_suffix="jsonb", cast="jsonb")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _convert(bind, tmp_suffix="json", cast="json")
