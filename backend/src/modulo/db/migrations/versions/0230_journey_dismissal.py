"""Operator dismissal (soft-delete) columns for ``journeys`` (FAR-795 slice C).

Revision ID: 0230_journey_dismissal
Revises: 0229_add_workspace_inputs_count
Create Date: 2026-09-13

Schema legs (Postgres only; SQLite/ORM-created test schemas get the columns
and the partial index from the ``Journey`` model's ``create_all``):

1. ``journeys.dismissed_at`` — nullable ``timestamptz``; set by the operator
   dismiss action, cleared by the restore. A non-NULL value makes the row a
   TOMBSTONE: its canonical ``(organisation_id, kind, ref)`` key still
   occupies ``uq_journeys_org_kind_ref`` so the mint upserts can tell the row
   exists, but every mint/advance conflict arm and every read path filters
   ``dismissed_at IS NULL``.
2. ``journeys.dismissed_by`` — nullable ``uuid`` — the operator account.
3. ``journeys.reason`` — nullable ``text``.
4. ``ix_journeys_active_org_kind_ref`` — btree ``(organisation_id, kind,
   ref)`` ``WHERE dismissed_at IS NULL``: the exact shape of the
   drift-predicate joins and the listing lookups, so both can skip dismissed
   rows entirely. Plain ``CREATE INDEX IF NOT EXISTS`` (index sizes are
   bounded by the org's journey count; no CONCURRENTLY inside the Alembic
   transaction — the 0147 precedent).

No data legs: NULL (never dismissed) is the semantically correct initial
state for every existing row. Dismissal must never be backfilled.

Downgrade: drops the index + the three columns. The tombstones those columns
hid would abruptly reappear as live journey rows — acceptable for a slice-C
downgrade (mirrors the 0222 downgrade posture).
"""

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

revision = "0230_journey_dismissal"
down_revision = "0229_add_workspace_inputs_count"
branch_labels = None
depends_on = None

_columns = [
    '"dismissed_at" timestamptz',
    '"dismissed_by" uuid',
    '"reason" text',
]


def _is_postgres(bind: Connection) -> bool:
    return str(bind.dialect.name).startswith("postgres")


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        # SQLite (ORM-created unit-test schemas) gets the columns + partial
        # index from the Journey model's create_all.
        return

    # --- columns (existence-guarded — re-runs are no-ops) ---
    for column in _columns:
        name = column.split(" ")[0]
        column_name = name.strip('"')
        have = bind.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'journeys' AND column_name = :column_name"
            ),
            {"column_name": column_name},
        ).scalar()
        if not have:
            bind.execute(text(f"ALTER TABLE public.journeys ADD COLUMN {column}"))  # nosec B608

    # --- partial index (IF NOT EXISTS — idempotent) ---
    bind.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_journeys_active_org_kind_ref "
            "ON public.journeys USING btree (organisation_id, kind, ref) "
            "WHERE dismissed_at IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    bind.execute(text("DROP INDEX IF EXISTS ix_journeys_active_org_kind_ref"))
    for column in _columns:
        name = column.split(" ")[0]
        bind.execute(text(f"ALTER TABLE public.journeys DROP COLUMN IF EXISTS {name}"))  # nosec B608
