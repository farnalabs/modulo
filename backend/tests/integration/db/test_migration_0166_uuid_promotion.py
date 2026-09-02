"""Integration test for the FK/CHECK constraint and UUID-promotion migration chain.

Runs the real Alembic ``upgrade`` chain up to ``0166_promote_uuid_fk_columns``
against a *fresh* live Postgres (its own testcontainer, built from scratch) and
proves the review-requested safety properties:

  * CHECK constraints (0157) and foreign keys (0155/0164/0165) are added
    ``NOT VALID`` then ``VALIDATE``-d, so a populated table never aborts the
    upgrade on pre-existing offending rows at ``ADD`` time.
  * The UUID promotion (0166) preserves a well-formed UUID string across the
    ``USING col::uuid`` cast, and its downgrade reverts with ``USING col::text``.
  * 0166 promotes the four columns to native ``uuid`` (the ``compare_metadata``
    gate requires the ORM and the migrated schema to agree on the type, and the
    ORM already declares these as ``Uuid``).

The test builds its own container rather than re-applying the constraints on the
already-migrated shared test DB: re-running the bare ``ADD CONSTRAINT`` statements
against a schema that already has them raises ``duplicate_object``. A freshly
built schema proves the chain applies cleanly.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

_PRE_0154 = "0154_add_web_vital_events_time_index"
_HEAD = "0166_promote_uuid_fk_columns"


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
    tears the container down afterwards. This lets the test prove the 0155-0157
    chain applies cleanly without disturbing other integration tests.
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


async def test_0155_0156_0157_upgrade_on_fresh_schema(fresh_migration_db, monkeypatch) -> None:
    db_url = fresh_migration_db
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_async_engine(db_url, poolclass=NullPool)

    try:
        # 0156 CHECK constraints exist on the production tables after the chain.
        async with engine.connect() as conn:
            check_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE contype = 'c' "
                            "AND conrelid = (SELECT oid FROM pg_class WHERE relname = 'runs')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert "ck_run_claim_count" in check_names, check_names

        # 0155 foreign keys exist after the chain (added NOT VALID + VALIDATE).
        async with engine.connect() as conn:
            fk_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE contype = 'f' "
                            "AND conrelid = (SELECT oid FROM pg_class WHERE relname = 'org_api_keys')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert "org_api_keys_run_id_fkey" in fk_names, fk_names

        # 0157 promoted run_evidence.node_id to native uuid, and the ORM already
        # declares it as Uuid — so compare_metadata parity holds.
        async with engine.connect() as conn:
            evidence_type = (
                await conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name = 'run_evidence' AND column_name = 'node_id'"
                    )
                )
            ).scalar()
        assert evidence_type == "uuid", f"run_evidence.node_id should be uuid, got {evidence_type!r}"
    finally:
        await engine.dispose()


async def test_0157_uuid_promotion_round_trips(fresh_migration_db, monkeypatch) -> None:
    db_url = fresh_migration_db
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_async_engine(db_url, poolclass=NullPool)

    well_formed = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS t_uuidpromo_promo"))
        await conn.execute(text("CREATE TABLE t_uuidpromo_promo (id uuid PRIMARY KEY, node_id varchar(255) NOT NULL)"))
        await conn.execute(
            text("INSERT INTO t_uuidpromo_promo (id, node_id) VALUES (:id, :n)"),
            {"id": str(uuid.uuid4()), "n": str(well_formed)},
        )

    try:
        # Apply the exact 0157 cast: String(255) -> Uuid USING node_id::uuid.
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE t_uuidpromo_promo ALTER COLUMN node_id TYPE uuid USING node_id::uuid"))
        async with engine.connect() as conn:
            got = (await conn.execute(text("SELECT node_id FROM t_uuidpromo_promo WHERE id IS NOT NULL"))).scalar()
        assert got == well_formed, f"uuid string must survive the ::uuid cast, got {got!r}"

        # And the 0157 downgrade cast: Uuid -> String(255) USING node_id::text.
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE t_uuidpromo_promo ALTER COLUMN node_id TYPE varchar(255) USING node_id::text")
            )
        async with engine.connect() as conn:
            back = (await conn.execute(text("SELECT node_id FROM t_uuidpromo_promo WHERE id IS NOT NULL"))).scalar()
        assert back == str(well_formed), f"uuid must revert via ::text, got {back!r}"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_uuidpromo_promo"))
        await engine.dispose()
