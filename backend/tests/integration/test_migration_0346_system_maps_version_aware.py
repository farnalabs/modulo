"""FAR-2288: migration ``0346_system_maps_version_aware`` backfill.

Against REAL Postgres (testcontainers, private database per test - the
``test_migration_0308`` harness). Seeds the pre-0346 shape at
``0345_system_maps_name_integrity``, runs the REAL upgrade, and asserts the
backfilled effective windows:

* a superseded (archived, with a successor) revision is closed at the
  successor's ``effective_from``;
* the current published revision keeps ``effective_to = NULL``;
* the NEWEST revision, when explicitly archived with no successor, is closed
  at its ``updated_at`` rather than left open-ended - otherwise a by-time read
  would report an archived revision as active at every future timestamp.

The newest-archived assertion fails against the previous backfill, which set
``LEAD(...)`` as ``effective_to`` and therefore stamped NULL for the last row
of the partition regardless of status.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]  # backend/
MIGRATION_REV = "0346_system_maps_version_aware"
PREV_REV = "0345_system_maps_name_integrity"

_ORG = "e0e00000-0000-0000-0000-000000000001"
_LATEST_ARCHIVED_MAP = "e0e00000-0000-0000-0000-000000000002"
_SUPERSEDED_MAP = "e0e00000-0000-0000-0000-000000000003"

_A_REV1 = "e0e00000-0000-0000-0000-000000000011"
_A_REV2 = "e0e00000-0000-0000-0000-000000000012"
_B_REV1 = "e0e00000-0000-0000-0000-000000000021"
_B_REV2 = "e0e00000-0000-0000-0000-000000000022"

_T1 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_T2 = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
_T3 = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


def _alembic_cmd(cmd: str, target: str) -> None:
    """Run an alembic command; env.py resolves the URL from the environment."""
    config = _alembic_config("postgresql+asyncpg://unused/unused")
    assert getattr(config, "cmd_opts", None) is None
    getattr(command, cmd)(config, target)


async def _fresh_db(db_url: str, prefix: str) -> tuple[str, str]:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    db_name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}" WITH TEMPLATE template0'))
    await admin_engine.dispose()
    return db_name, _swap_db_name(db_url, db_name)


async def _drop_db(db_url: str, db_name: str) -> None:
    admin_engine = create_async_engine(db_url, poolclass=NullPool, execution_options={"isolation_level": "AUTOCOMMIT"})
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


async def _ensure_alembic_table(iso_url: str) -> None:
    engine = create_async_engine(iso_url, poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
        )
    await engine.dispose()


def _migrate(iso_url: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        _alembic_cmd("upgrade", target)


async def _seed_pre_upgrade(iso_url: str) -> None:
    """Seed the pre-0346 revision timeline.

    ``session_replication_role = replica`` switches FK triggers off for the seed
    connection only, so the fixture does not have to construct a whole
    organisation/map graph; the migration's data behaviour is the subject.

    ``_LATEST_ARCHIVED_MAP`` is the defect case: its newest revision was
    explicitly archived and has no successor. ``_SUPERSEDED_MAP`` is the
    normal case: an archived superseded revision plus the current published one.
    """
    engine = create_async_engine(iso_url, poolclass=NullPool)
    rows = (
        (_A_REV1, _LATEST_ARCHIVED_MAP, 1, "published", _T1, _T1),
        (_A_REV2, _LATEST_ARCHIVED_MAP, 2, "archived", _T2, _T3),
        (_B_REV1, _SUPERSEDED_MAP, 1, "archived", _T1, _T2),
        (_B_REV2, _SUPERSEDED_MAP, 2, "published", _T2, _T2),
    )
    async with engine.begin() as conn:
        await conn.execute(text("SET session_replication_role = replica"))
        for rev_id, map_id, number, status, published_at, updated_at in rows:
            await conn.execute(
                text(
                    "INSERT INTO system_map_revisions "
                    "(id, organisation_id, system_map_id, revision_number, status, content_json, "
                    " published_at, created_at, updated_at) "
                    "VALUES (:id, :org, :map, :number, :status, '{}'::jsonb, "
                    " :published_at, :created_at, :updated_at)"
                ),
                {
                    "id": rev_id,
                    "org": _ORG,
                    "map": map_id,
                    "number": number,
                    "status": status,
                    "published_at": published_at,
                    "created_at": published_at,
                    "updated_at": updated_at,
                },
            )
    await engine.dispose()


async def _effective_columns_present(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'system_map_revisions' "
                "AND column_name IN ('effective_from', 'effective_to')"
            )
        )
        return len(result.all()) == 2


async def _windows(engine: AsyncEngine) -> dict[str, dict[str, object]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, status, effective_from, effective_to FROM system_map_revisions"))
        ).all()
    return {
        str(row.id): {
            "status": row.status,
            "effective_from": row.effective_from,
            "effective_to": row.effective_to,
        }
        for row in rows
    }


async def _by_time(engine: AsyncEngine, map_id: str, at: datetime) -> str | None:
    """The migration's by-time predicate, mirroring ``get_revision_by_time``."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id FROM system_map_revisions "
                "WHERE system_map_id = :map AND effective_from <= :at "
                "AND (effective_to IS NULL OR :at < effective_to) "
                "ORDER BY effective_from DESC LIMIT 1"
            ),
            {"map": map_id, "at": at},
        )
        found = result.scalar_one_or_none()
    return None if found is None else str(found)


@pytest_asyncio.fixture
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    db_name, iso_url = await _fresh_db(db_url, "m0346_pre")
    try:
        await _ensure_alembic_table(iso_url)
        _migrate(iso_url, PREV_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def test_newest_archived_revision_gets_a_closed_window(pre_upgrade_db_url: str) -> None:
    """The last archived revision must not keep an open-ended window."""
    engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
    try:
        # NEGATIVE CONTROL: at 0345 neither column exists, so the assertions
        # below cannot pass without the real 0346 upgrade.
        assert not await _effective_columns_present(engine)

        await _seed_pre_upgrade(pre_upgrade_db_url)
        _migrate(pre_upgrade_db_url, MIGRATION_REV)
        assert await _effective_columns_present(engine)

        windows = await _windows(engine)

        superseded = windows[_A_REV1]
        assert superseded["effective_from"] == _T1
        assert superseded["effective_to"] == _T2, "a superseded revision closes at its successor's effective_from"

        archived = windows[_A_REV2]
        assert archived["status"] == "archived"
        assert archived["effective_from"] == _T2
        assert archived["effective_to"] is not None, (
            "an archived revision with no successor must be closed, not left open-ended"
        )
        assert archived["effective_to"] == _T3, "the archive time (updated_at) closes the window"

        # After the archive instant the map has no active revision.
        after_archive = await _by_time(engine, _LATEST_ARCHIVED_MAP, _T3 + timedelta(days=1))
        assert after_archive is None, "an archived revision must not be returned as active in the future"

        # But it is still reachable inside its historical window.
        inside = await _by_time(engine, _LATEST_ARCHIVED_MAP, _T2 + timedelta(hours=1))
        assert inside == _A_REV2
    finally:
        await engine.dispose()


async def test_superseded_and_current_windows(pre_upgrade_db_url: str) -> None:
    """Superseded revisions close; the current published revision stays open."""
    engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
    try:
        await _seed_pre_upgrade(pre_upgrade_db_url)
        _migrate(pre_upgrade_db_url, MIGRATION_REV)

        windows = await _windows(engine)

        superseded = windows[_B_REV1]
        assert superseded["effective_from"] == _T1
        assert superseded["effective_to"] == _T2

        current = windows[_B_REV2]
        assert current["status"] == "published"
        assert current["effective_from"] == _T2
        assert current["effective_to"] is None, "the current published revision has an open-ended window"

        assert await _by_time(engine, _SUPERSEDED_MAP, _T1 + timedelta(hours=1)) == _B_REV1
        assert await _by_time(engine, _SUPERSEDED_MAP, _T2 + timedelta(hours=1)) == _B_REV2
        assert await _by_time(engine, _SUPERSEDED_MAP, _T1 - timedelta(hours=1)) is None
    finally:
        await engine.dispose()
