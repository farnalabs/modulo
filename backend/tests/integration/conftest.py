"""Integration test fixtures — spins up a real Postgres via Testcontainers."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

# Collection imports the FastAPI app before database fixtures run. Provide only
# test-local defaults here so standalone collection never depends on a caller's
# shell environment. ``setdefault`` still lets CI supply explicit values.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/modulo_integration")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

BACKEND_ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Generator[None, None, None]:
    """Keep settings derived from one integration test out of the next."""
    from modulo.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _domain_table_names(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
        return names - {"alembic_version"}
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def db_url(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url()
    # Convert to asyncpg driver
    return url.replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def migrated_db_url(db_url: str) -> str:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)

    async def _ensure_alembic_table() -> None:
        """Pre-create alembic_version with VARCHAR(255) to support branch migration IDs."""
        eng = create_async_engine(db_url)
        async with eng.connect() as conn:
            await conn.execute(
                text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"),
            )
            await conn.commit()
        await eng.dispose()

    asyncio.run(_ensure_alembic_table())
    # Override DATABASE_URL so alembic env.py uses the testcontainer, not any
    # CI env var pointing at the service postgres.
    os.environ["DATABASE_URL"] = db_url
    command.upgrade(config, "heads")

    async def _existing_cols(conn: Any, table: str) -> set[str]:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :tbl AND table_schema = 'public'",
            ),
            {"tbl": table},
        )
        return {row[0] for row in result.fetchall()}

    async def _patch_schema() -> None:
        """Add ORM columns missing from migrations to make CRUD functions work."""
        eng = create_async_engine(db_url)
        async with eng.connect() as conn:
            # pipelines: missing default_autonomy_level
            cols = await _existing_cols(conn, "pipelines")
            if "default_autonomy_level" not in cols:
                await conn.execute(
                    text(
                        "ALTER TABLE pipelines ADD COLUMN default_autonomy_level VARCHAR(30) DEFAULT 'manual_approval'",
                    ),
                )

            # organisations: otel_config_json has no server default, causing NOT NULL
            # violations on raw SQL INSERTs that don't include the column.
            cols = await _existing_cols(conn, "organisations")
            if "otel_config_json" in cols:
                await conn.execute(
                    text("ALTER TABLE organisations ALTER COLUMN otel_config_json SET DEFAULT '{}'::json"),
                )

            # webhook_payloads: ORM expects raw_body + raw_payload (migration has payload_ciphertext)
            cols = await _existing_cols(conn, "webhook_payloads")
            if "raw_body" not in cols:
                await conn.execute(text("ALTER TABLE webhook_payloads ADD COLUMN raw_body BYTEA"))
            if "raw_payload" not in cols:
                await conn.execute(text("ALTER TABLE webhook_payloads ADD COLUMN raw_payload JSON"))
            if "payload_ciphertext" in cols:
                await conn.execute(text("ALTER TABLE webhook_payloads ALTER COLUMN payload_ciphertext DROP NOT NULL"))

            # Force RLS on all org-scoped tables so it applies to the testcontainers
            # superuser role too. In production, the modulo_app role is not a superuser
            # so RLS applies automatically — but testcontainers run as the DB superuser
            # which bypasses ENABLE RLS, hence the explicit FORCE in tests.
            for _tbl in (
                "org_daily_run_counts",
                "org_memberships",
                "audit_events",
                "schemas",
                "teams",
                "connector_instances",
                "library_primitives",
                "model_backends",
                "org_api_keys",
                "schema_versions",
                "stages",
                "agents",
                "pipelines",
                "pipeline_edges",
                "pipeline_snapshots",
                "triggers",
                "runs",
                "webhook_dedup_hashes",
                "hitl_claims",
                "notification_delivery_log",
                "trigger_events",
                "webhook_payloads",
            ):
                await conn.execute(text(f"ALTER TABLE {_tbl} FORCE ROW LEVEL SECURITY"))

            await conn.commit()
        await eng.dispose()

    asyncio.run(_patch_schema())
    return db_url


@pytest_asyncio.fixture(scope="session")
async def db_engine(migrated_db_url: str) -> AsyncEngine:
    engine = create_async_engine(migrated_db_url, echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncSession:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# Shared entity fixtures — session-scoped to avoid recreating for every test
# These are inherited by all subdirectories (crud/, bdd/, feedback_manager/,
# trigger_engine/) via pytest conftest discovery.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def test_org(db_engine: AsyncEngine) -> uuid.UUID:
    """Committed organisation row available for the whole test session."""
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Integration Test Org",
                "slug": f"int-{org_id.hex[:8]}",
            },
        )
    return org_id


@pytest_asyncio.fixture(scope="session")
async def test_user(db_engine: AsyncEngine, test_org: uuid.UUID) -> uuid.UUID:
    """Committed account + org_membership row in test_org."""
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {
                "id": str(account_id),
                "email": "integration-test@example.com",
                "name": "Integration Test User",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {
                "mid": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(test_org),
            },
        )
    return account_id


@pytest_asyncio.fixture
async def rls_session(db_engine: AsyncEngine, test_org: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    """AsyncSession with RLS set to test_org; all ORM changes are rolled back."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text("SELECT 1"))
        from modulo.db.rls import set_rls_org

        await set_rls_org(session, test_org)
        yield session
        await session.rollback()


@pytest_asyncio.fixture(scope="session")
async def test_pipeline(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> uuid.UUID:
    """Committed pipeline row in test_org."""
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(test_org),
                "name": "Integration Test Pipeline",
                "uid": str(test_user),
            },
        )
    return pipeline_id


@pytest_asyncio.fixture(scope="session")
async def test_snapshot(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
) -> uuid.UUID:
    """Committed pipeline_snapshot row in test_org."""
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(test_pipeline), "oid": str(test_org)},
        )
    return snapshot_id


@pytest_asyncio.fixture(scope="session")
async def test_trigger(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_user: uuid.UUID,
) -> uuid.UUID:
    """Committed trigger row (webhook type) in test_org."""
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, '{}'::json, :uid)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(test_org),
                "pid": str(test_pipeline),
                "uid": str(test_user),
            },
        )
    return trigger_id
