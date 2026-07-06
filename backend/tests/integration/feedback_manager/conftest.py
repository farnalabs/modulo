"""Shared fixtures for Feedback integration tests.

Leverages the top-level db_engine, db_session, migrated_db_url fixtures
from tests/integration/conftest.py (Postgres via testcontainers).
Provides test_org, test_user, and rls_session at the session/module level.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest_asyncio.fixture(scope="module")
async def test_org(db_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "Feedback Test Org",
                "slug": f"fb-test-{org_id.hex[:8]}",
            },
        )
    return org_id


@pytest_asyncio.fixture(scope="module")
async def test_user(db_engine: AsyncEngine, test_org: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO users (id, organisation_id, email, display_name) VALUES (:id, :org_id, :email, :name)",
            ),
            {
                "id": str(user_id),
                "org_id": str(test_org),
                "email": "feedback-test@example.com",
                "name": "Feedback Test User",
            },
        )
    return user_id


@pytest_asyncio.fixture
async def rls_session(db_engine: AsyncEngine, test_org: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text("SELECT 1"))
        from modulo.db.rls import set_rls_org
        await set_rls_org(session, test_org)
        yield session
        await session.rollback()
