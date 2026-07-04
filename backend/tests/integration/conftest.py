"""Integration test fixtures — spins up a real Postgres via Testcontainers."""

import asyncio
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

BACKEND_ROOT = Path(__file__).parents[2]


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
    with PostgresContainer("postgres:16-alpine", startup_timeout=120) as pg:
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
                "SELECT column_name FROM information_schema.columns WHERE table_name = :tbl AND table_schema = 'public'",
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

            # webhook_payloads: ORM expects raw_body + raw_payload (migration has payload_ciphertext)
            cols = await _existing_cols(conn, "webhook_payloads")
            if "raw_body" not in cols:
                await conn.execute(text("ALTER TABLE webhook_payloads ADD COLUMN raw_body BYTEA"))
            if "raw_payload" not in cols:
                await conn.execute(text("ALTER TABLE webhook_payloads ADD COLUMN raw_payload JSON"))
            if "payload_ciphertext" in cols:
                await conn.execute(text("ALTER TABLE webhook_payloads ALTER COLUMN payload_ciphertext DROP NOT NULL"))

            # Force RLS on all org-scoped tables so it applies to the testcontainers
            # superuser too. Without FORCE, PostgreSQL superusers bypass ENABLE RLS,
            # which breaks cross-tenant isolation tests that rely on SET LOCAL ROLE.
            for _tbl in (
                "org_daily_run_counts",
                "users",
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
