"""Unit tests for the 0347 backfill's dialect-independent path.

The Postgres CTE is covered against real Postgres by
``tests/integration/test_migration_0347_system_maps_version_aware.py``. This
module calls ``_backfill_effective_dates(bind, pg=False)`` directly against an
in-memory SQLite database, pinning the fallback loop (which CI's Postgres-only
integration lane never executes) without Docker:

* a superseded (archived, with a successor) revision closes at the successor's
  ``effective_from``;
* the newest published revision keeps ``effective_to = NULL``;
* the newest revision, when it was published and later archived, closes at its
  ``updated_at`` rather than being left open-ended;
* a revision archived without ever being published gets no window at all and
  does not truncate the published revision's open-ended window;
* soft-deleted rows are untouched;
* an empty table is a no-op.

SQLite stores ``DateTime`` as TEXT, so this module uses naive UTC datetimes to
keep the comparison unambiguous; the SQL is dialect-independent.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "farna" / "db" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / "0347_system_maps_version_aware.py"

_T1 = datetime(2026, 1, 1, 12, 0)
_T2 = datetime(2026, 1, 2, 12, 0)
_T3 = datetime(2026, 1, 3, 12, 0)
_T4 = datetime(2026, 1, 4, 12, 0)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0347_system_maps_version_aware", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revisions_table(metadata: sa.MetaData) -> sa.Table:
    return sa.Table(
        "system_map_revisions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("system_map_id", sa.String, nullable=False),
        sa.Column("revision_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
    )


def _row(
    rev_id: str,
    *,
    number: int,
    status: str,
    published_at: datetime | None,
    created_at: datetime,
    updated_at: datetime,
    deleted_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": rev_id,
        "system_map_id": "map-1",
        "revision_number": number,
        "status": status,
        "published_at": published_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "deleted_at": deleted_at,
    }


@contextmanager
def _backfilled(rows: Sequence[dict[str, Any]]) -> Iterator[dict[str, tuple[Any, Any]]]:
    """Seed an in-memory DB, run the fallback backfill, yield {id: (from, to)}."""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = _revisions_table(metadata)
    metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            if rows:
                conn.execute(table.insert(), list(rows))
            _load_migration()._backfill_effective_dates(conn, pg=False)
        with engine.connect() as conn:
            selected = conn.execute(sa.select(table.c.id, table.c.effective_from, table.c.effective_to)).all()
        yield {row.id: (row.effective_from, row.effective_to) for row in selected}
    finally:
        engine.dispose()


def test_backfill_closes_superseded_and_keeps_published_open() -> None:
    """A successor closes the previous window; the published revision stays open."""
    with _backfilled(
        [
            _row("rev1", number=1, status="archived", published_at=_T1, created_at=_T1, updated_at=_T2),
            _row("rev2", number=2, status="published", published_at=_T2, created_at=_T2, updated_at=_T2),
        ]
    ) as windows:
        assert windows["rev1"] == (_T1, _T2)
        assert windows["rev2"] == (_T2, None)


def test_backfill_closes_a_newest_archived_revision() -> None:
    """The newest revision, when archived, is closed at its superseded timestamp."""
    with _backfilled(
        [
            _row("rev1", number=1, status="archived", published_at=_T1, created_at=_T1, updated_at=_T2),
            _row("rev2", number=2, status="archived", published_at=_T2, created_at=_T2, updated_at=_T3),
        ]
    ) as windows:
        assert windows["rev1"] == (_T1, _T2)
        assert windows["rev2"] == (_T2, _T3)


def test_backfill_ignores_a_never_published_archived_revision() -> None:
    """A draft archived without a publish has no window and truncates nothing."""
    with _backfilled(
        [
            _row("rev1", number=1, status="archived", published_at=_T1, created_at=_T1, updated_at=_T2),
            _row("rev2", number=2, status="published", published_at=_T2, created_at=_T2, updated_at=_T2),
            # Never published: created at T3, archived at T4.
            _row("rev3", number=3, status="archived", published_at=None, created_at=_T3, updated_at=_T4),
        ]
    ) as windows:
        assert windows["rev3"] == (None, None)
        assert windows["rev2"] == (_T2, None), "a never-published revision must not close the published window"


def test_backfill_skips_soft_deleted_revisions() -> None:
    """Soft-deleted rows are not part of the timeline and stay untouched."""
    with _backfilled(
        [
            _row("live", number=1, status="published", published_at=_T1, created_at=_T1, updated_at=_T1),
            _row(
                "deleted",
                number=2,
                status="archived",
                published_at=_T2,
                created_at=_T2,
                updated_at=_T3,
                deleted_at=_T3,
            ),
        ]
    ) as windows:
        assert windows["deleted"] == (None, None)
        assert windows["live"] == (_T1, None)


def test_backfill_on_empty_table_is_a_noop() -> None:
    """A database with no revisions backfills cleanly."""
    with _backfilled([]) as windows:
        assert not windows
