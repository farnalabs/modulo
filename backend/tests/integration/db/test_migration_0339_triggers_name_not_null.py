"""Integration test for migration 0339_triggers_name_not_null (FAR-2227).

Against REAL Postgres (testcontainers, private database per test - the
``test_migration_0306`` harness): seed legacy trigger rows at
``0338_environment_profile_max_node_seconds`` (``name`` still nullable), run the
REAL alembic ``upgrade`` and prove:

* every NULL name - live AND soft-deleted - is backfilled with
  ``<trigger_type>-<first 8 hex chars of the id>``;
* a candidate that would collide with a live name on the same pipeline falls
  back to the full 32-char hex id (the identity index would otherwise reject
  the backfill);
* an explicit name is never touched;
* ``triggers.name`` is NOT NULL afterwards (a NULL insert is rejected by
  Postgres) and the downgrade makes it nullable again.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

MIGRATION_REV = "0339_triggers_name_not_null"
PREV_REV = "0338_environment_profile_max_node_seconds"

_LIVE_WEBHOOK = uuid.UUID("aaaaaaaa-1111-2222-3333-444444444444")
_LIVE_CRON = uuid.UUID("bbbbbbbb-1111-2222-3333-444444444444")
_DELETED_MANUAL = uuid.UUID("cccccccc-1111-2222-3333-444444444444")
_COLLIDING_NULL = uuid.UUID("dddddddd-1111-2222-3333-444444444444")
_EXPLICIT_NAME = uuid.UUID("eeeeeeee-1111-2222-3333-444444444444")
_OCCUPIES_CANDIDATE = uuid.UUID("ffffffff-1111-2222-3333-444444444444")


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _swap_db_name(db_url: str, new_db: str) -> str:
    parsed = urlparse(db_url)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


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


def _run_alembic(iso_url: str, cmd: str, target: str) -> None:
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_URL", iso_url)
        mp.setenv("DATABASE_ADMIN_URL", iso_url)
        getattr(command, cmd)(_alembic_config(iso_url), target)


@pytest_asyncio.fixture
async def pre_upgrade_db_url(db_url: str) -> AsyncGenerator[str, None]:
    db_name, iso_url = await _fresh_db(db_url, "m0339_pre")
    try:
        _run_alembic(iso_url, "upgrade", PREV_REV)
        yield iso_url
    finally:
        await _drop_db(db_url, db_name)


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    ids = {"account": uuid.uuid4(), "org": uuid.uuid4(), "pipeline": uuid.uuid4(), "other_pipeline": uuid.uuid4()}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, 'f2227@example.com', 'f2227', 'hash', 'local', true)"
            ),
            {"id": str(ids["account"])},
        )
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'f2227', 'f2227', '{}'::json)"
            ),
            {"id": str(ids["org"])},
        )
        for key in ("pipeline", "other_pipeline"):
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, account_id, name, "
                    "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                    "run_context_defaults, graph_nodes_json) "
                    "VALUES (:id, :oid, :aid, :name, 10, 30, 300, '{}'::json, '[]'::json)"
                ),
                {"id": str(ids[key]), "oid": str(ids["org"]), "aid": str(ids["account"]), "name": f"f2227 {key}"},
            )
        rows: list[tuple[uuid.UUID, str, str, str | None, bool]] = [
            # (id, pipeline key, trigger_type, name, deleted)
            (_LIVE_WEBHOOK, "pipeline", "webhook", None, False),
            (_LIVE_CRON, "pipeline", "cron", None, False),
            (_DELETED_MANUAL, "pipeline", "manual", None, True),
            (_EXPLICIT_NAME, "pipeline", "cron", "nightly", False),
            # A live trigger already holding the name the backfill would give _COLLIDING_NULL.
            (_OCCUPIES_CANDIDATE, "other_pipeline", "webhook", None, False),
            (_COLLIDING_NULL, "other_pipeline", "webhook", None, False),
        ]
        for row_id, pipeline_key, trigger_type, name, deleted in rows:
            await conn.execute(
                text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, account_id, trigger_type, name, "
                    "config_json, deleted_at) "
                    "VALUES (:id, :oid, :pid, :aid, :tt, :name, '{}'::json, "
                    "CASE WHEN :deleted THEN now() ELSE NULL END)"
                ),
                {
                    "id": str(row_id),
                    "oid": str(ids["org"]),
                    "pid": str(ids[pipeline_key]),
                    "aid": str(ids["account"]),
                    "tt": trigger_type,
                    "name": name,
                    "deleted": deleted,
                },
            )
        # Make the colliding candidate real: _OCCUPIES_CANDIDATE takes the exact
        # name the short backfill would otherwise give _COLLIDING_NULL.
        await conn.execute(
            text("UPDATE triggers SET name = 'webhook-dddddddd' WHERE id = :id"),
            {"id": str(_OCCUPIES_CANDIDATE)},
        )
    return ids


async def _names(engine: AsyncEngine) -> dict[uuid.UUID, str | None]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT id, name FROM triggers"))
        return {uuid.UUID(str(row[0])): row[1] for row in rows.all()}


async def _name_is_nullable(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'triggers' AND column_name = 'name'"
                )
            )
        ).scalar_one()
    return str(value) == "YES"


async def test_upgrade_backfills_null_names_and_enforces_not_null(pre_upgrade_db_url: str) -> None:
    engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
    try:
        ids = await _seed(engine)
        assert await _name_is_nullable(engine) is True

        _run_alembic(pre_upgrade_db_url, "upgrade", MIGRATION_REV)

        names = await _names(engine)
        assert names[_LIVE_WEBHOOK] == "webhook-aaaaaaaa"
        assert names[_LIVE_CRON] == "cron-bbbbbbbb"
        assert names[_DELETED_MANUAL] == "manual-cccccccc"
        assert names[_EXPLICIT_NAME] == "nightly"
        assert names[_OCCUPIES_CANDIDATE] == "webhook-dddddddd"
        # The short candidate was taken on this pipeline: full hex id fallback.
        assert names[_COLLIDING_NULL] == f"webhook-{_COLLIDING_NULL.hex}"
        assert None not in names.values()

        assert await _name_is_nullable(engine) is False
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO triggers (id, organisation_id, pipeline_id, account_id, trigger_type, name, "
                        "config_json) VALUES (:id, :oid, :pid, :aid, 'manual', NULL, '{}'::json)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "oid": str(ids["org"]),
                        "pid": str(ids["pipeline"]),
                        "aid": str(ids["account"]),
                    },
                )
    finally:
        await engine.dispose()


async def test_downgrade_makes_name_nullable_again(pre_upgrade_db_url: str) -> None:
    engine = create_async_engine(pre_upgrade_db_url, poolclass=NullPool)
    try:
        await _seed(engine)
        _run_alembic(pre_upgrade_db_url, "upgrade", MIGRATION_REV)
        assert await _name_is_nullable(engine) is False

        _run_alembic(pre_upgrade_db_url, "downgrade", PREV_REV)

        assert await _name_is_nullable(engine) is True
        # Backfilled names stay: they are valid names and dropping them would lose information.
        assert (await _names(engine))[_LIVE_WEBHOOK] == "webhook-aaaaaaaa"
    finally:
        await engine.dispose()
