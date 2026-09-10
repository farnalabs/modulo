"""Integration test for migration 0194_uuid_pk_server_defaults (FAR-718).

Migration 0191 froze all deploys because a raw-SQL INSERT omitted ``id`` and
the table's uuid PK had no DB server default (the ORM always supplies ids
client-side, so only raw-SQL migrations hit this). 0194 adds a
``gen_random_uuid()`` server default to EVERY uuid primary key, killing the
whole bug class.

Runs the real Alembic ``upgrade`` chain up to ``0194_uuid_pk_server_defaults``
against a *fresh* live Postgres (its own testcontainer, built from scratch)
and proves:

  * every uuid PK column (ORM metadata, FK parents excluded) carries the
    ``gen_random_uuid()`` default in ``information_schema.columns``;
  * the default is SELECTIVE — string PKs and composite-PK FK columns
    (``run_evidence``) are untouched;
  * the outage-class path is fixed: a raw ``INSERT INTO system_config ...``
    WITHOUT the id column SUCCEEDS and gets a server-generated uuid;
  * the downgrade drops the defaults and a re-upgrade restores them.

The test builds its own container rather than reusing the shared session DB:
the default must hold on a freshly migrated chain, not only on whatever state
the shared DB happens to be in.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Uuid, text
from sqlalchemy.dialects.postgresql import UUID as POSTGRES_UUID
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from modulo.db.models import Base

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

_HEAD = "0194_uuid_pk_server_defaults"
_PRE_HEAD = "0193_run_node_outputs_sweep_index"


def _uuid_pk_pairs(existing: set[str] | None = None) -> set[tuple[str, str]]:
    """(table, column) for every uuid primary key that is not a foreign-key parent.

    When ``existing`` is supplied, only pairs whose table is present in that set
    are returned. Migration 0194 is applied against a DB migrated only to its own
    head, so tables introduced by LATER migrations (e.g. ``runner_probe_cache`` at
    0204) do not exist yet at that state and must not be asserted against here —
    those migrations supply their own uuid-PK server default at CREATE time.
    """
    pairs: set[tuple[str, str]] = set()
    for table in Base.metadata.sorted_tables:
        if existing is not None and table.name not in existing:
            continue
        fk_parents = {fk.parent.name for fk in table.foreign_keys}
        for column in table.columns:
            if column.primary_key and isinstance(column.type, (Uuid, POSTGRES_UUID)) and column.name not in fk_parents:
                pairs.add((table.name, column.name))
    return pairs


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


def _with_credentials(database_url: str, user: str, password: str) -> str:
    from urllib.parse import quote

    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{quote(user)}:{quote(password)}@{host}/{db}"


@pytest.fixture
def fresh_migration_db(monkeypatch):
    """A freshly migrated DB built from scratch for migration-chain assertions.

    Spins up its own Postgres container (independent of the shared session DB),
    provisions the migration roles, runs ``alembic upgrade`` to ``_HEAD``, and
    tears the container down afterwards.

    ``DATABASE_URL`` is redirected to this ephemeral container for the duration
    of the test via ``monkeypatch.context()``, which restores it when the fixture
    tears down (before ``pg.stop()``). Consuming tests MUST NOT re-set
    ``DATABASE_URL`` themselves (see test_migration_0166_uuid_promotion.py for
    the full teardown-ordering rationale). Tests read the URL from this
    fixture's return value only.
    """
    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    raw = pg.get_connection_url().replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")

    async def _provision():
        eng = create_async_engine(raw)
        async with eng.connect() as conn:
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_migrate"'))
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_breakglass"'))
            await conn.execute(text('DROP ROLE IF EXISTS "modulo_app"'))
            await conn.execute(text("CREATE ROLE modulo_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
            await conn.execute(text("CREATE ROLE modulo_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
            await conn.execute(text("CREATE ROLE modulo_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'"))
            await conn.commit()
        await eng.dispose()

    asyncio_run(_provision())

    app_url = _with_credentials(raw, "modulo_app", "apppass")
    bg_url = _with_credentials(raw, "modulo_breakglass", "bgpass")
    config = _alembic_config(raw)
    with monkeypatch.context() as m:
        m.setenv("DATABASE_URL", raw)
        m.setenv("DATABASE_ADMIN_URL", raw)
        m.setenv("MODULO_BREAK_GLASS_DATABASE_URL", bg_url)
        from modulo.db.bootstrap_role import bootstrap_roles

        asyncio_run(bootstrap_roles(raw, app_url))
        command.upgrade(config, _HEAD)
        asyncio_run(bootstrap_roles(raw, app_url))
        yield raw
    pg.stop()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


async def test_every_uuid_pk_has_gen_random_uuid_default(fresh_migration_db) -> None:
    db_url = fresh_migration_db
    engine = create_async_engine(db_url, poolclass=NullPool)

    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT table_name, column_name, column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            )
            rows = {(r[0], r[1]): r[2] for r in result.fetchall()}
    finally:
        await engine.dispose()

    # Only check uuid-PK columns for tables the migration chain up to _HEAD
    # actually created. ORM metadata also lists tables added by LATER migrations
    # (e.g. runner_probe_cache in 0204), which this test does not build — those
    # are out of scope here and verified by their own migrations.
    present = {(t, c) for (t, c) in _uuid_pk_pairs() if (t, c) in rows}
    missing = sorted(f"{t}.{c}" for t, c in present if rows.get((t, c)) != "gen_random_uuid()")
    assert not missing, f"uuid PKs without gen_random_uuid() default: {missing}"

    # Selectivity: the default must NOT blanket-apply to non-uuid PKs or to the
    # composite-PK FK columns that were deliberately excluded.
    assert rows.get(("tier_catalog", "tier_id")) is None, "string PK must have no uuid default"
    assert rows.get(("run_evidence", "run_id")) is None, "FK composite-PK part must have no default"
    assert rows.get(("run_evidence", "node_id")) is None, "FK composite-PK part must have no default"


async def test_raw_insert_without_id_gets_server_generated_uuid(fresh_migration_db) -> None:
    """The outage-class proof (FAR-701/702): raw INSERT omitting id SUCCEEDS.

    Pre-0194 this exact statement shape failed with a NOT NULL violation on
    the id column and froze the migration chain. system_config has the minimum
    NOT NULL set (key, value; updated_at has its own server default) so the
    insert exercises the id default in isolation.
    """
    db_url = fresh_migration_db
    engine = create_async_engine(db_url, poolclass=NullPool)
    key = f"far-718-proof-{uuid.uuid4()}"

    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO system_config (key, value) VALUES (:key, :value)"),
                {"key": key, "value": "{}"},
            )
        async with engine.connect() as conn:
            generated = (
                await conn.execute(text("SELECT id FROM system_config WHERE key = :key"), {"key": key})
            ).scalar()
    finally:
        await engine.dispose()

    assert generated is not None, "raw INSERT without id must succeed post-0194"
    parsed = uuid.UUID(str(generated))
    assert parsed != uuid.UUID(int=0), "server default must generate a real uuid, not the nil uuid"
    assert parsed.version == 4, f"gen_random_uuid() yields v4, got version {parsed.version}"


async def test_downgrade_drops_defaults_and_reupgrade_restores(fresh_migration_db) -> None:
    db_url = fresh_migration_db
    engine = create_async_engine(db_url, poolclass=NullPool)
    config = _alembic_config(db_url)

    async def _snapshot() -> dict[tuple[str, str], str]:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT table_name, column_name, column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            )
            return {(r[0], r[1]): r[2] for r in result.fetchall()}

    pairs = _uuid_pk_pairs()

    def _count_defaults(rows: dict[tuple[str, str], str]) -> int:
        # Only count uuid-PK columns for tables the chain up to _HEAD created;
        # tables added by later migrations are out of scope (see test above).
        present = {p for p in pairs if p in rows}
        return sum(1 for t, c in present if rows.get((t, c)) == "gen_random_uuid()")

    try:
        # Programmatic command.downgrade() leaves cmd_opts unset, and env.py's
        # upgrade fast-path would skip a downgrade that starts at head — inject
        # the documented downgrade shape so the invocation is classified correctly.
        from types import SimpleNamespace

        config.cmd_opts = SimpleNamespace(command="downgrade")  # type: ignore[attr-defined]
        command.downgrade(config, _PRE_HEAD)
        del config.cmd_opts  # type: ignore[attr-defined]

        rows = await _snapshot()
        assert _count_defaults(rows) == 0, "downgrade must drop every uuid-PK default"

        command.upgrade(config, _HEAD)
        rows = await _snapshot()
        present = {p for p in pairs if p in rows}
        assert _count_defaults(rows) == len(present), "re-upgrade must restore every default"
    finally:
        await engine.dispose()
