"""Shared fixtures for CRUD integration tests.

The test_org and test_user rows are committed once at session scope.
The rls_session fixture provides an AsyncSession with RLS set to test_org;
all ORM changes within the session are rolled back after each test.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


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
                "name": "CRUD Test Org",
                "slug": f"crud-test-{org_id.hex[:8]}",
            },
        )
    return org_id


@pytest_asyncio.fixture(scope="session")
async def test_user(db_engine: AsyncEngine, test_org: uuid.UUID) -> uuid.UUID:
    """Committed user row in test_org, available for the whole test session."""
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO users (id, organisation_id, email, display_name) VALUES (:id, :org_id, :email, :name)",
            ),
            {
                "id": str(user_id),
                "org_id": str(test_org),
                "email": "crud-test@example.com",
                "name": "CRUD Test User",
            },
        )
    return user_id


@pytest_asyncio.fixture
async def rls_session(db_engine: AsyncEngine, test_org: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    """AsyncSession with RLS set to test_org; all ORM changes are rolled back."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        # Execute a query to trigger autobegin, then set RLS directly.
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(test_org)},
        )
        yield session
        await session.rollback()
