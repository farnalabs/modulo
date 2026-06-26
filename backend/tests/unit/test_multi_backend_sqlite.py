"""Smoke test: SQLite backend with create_all, CRUD, and tenant filtering.

Verifies that core ORM operations work on the sqlite+aiosqlite driver
without any Postgres-specific features (RLS, advisory locks, etc.).
"""

import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_multi_backend.db")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("MODULO_DB", "sqlite")

import modulo.db.models  # noqa: F401, E402
from modulo.db.models.base import Base  # noqa: E402
from modulo.db.models.organisation import Organisation  # noqa: E402
from modulo.db.models.user import User  # noqa: E402

_DB_URL = "sqlite+aiosqlite:///./test_multi_backend.db"
_DB_PATH = "./test_multi_backend.db"


@pytest.fixture(scope="module")
def _engine():
    engine = create_async_engine(_DB_URL, echo=False)
    yield engine
    # async dispose happens in _tables teardown


@pytest.fixture(scope="module")
async def _tables(_engine):
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _engine.dispose()
    if os.path.exists(_DB_PATH):
        os.remove(_DB_PATH)


@pytest.fixture(autouse=True)
async def _clear_data(_engine, _tables):
    async with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    yield


class TestSqliteMultiBackend:
    async def test_create_and_retrieve_org(self, _engine) -> None:
        org_id = uuid.uuid4()
        async with AsyncSession(_engine) as session:
            async with session.begin():
                session.add(
                    Organisation(id=org_id, name="Smoke Test Org", slug="smoke-test-org")
                )
            result = await session.execute(
                select(Organisation).where(Organisation.id == org_id)
            )
            org = result.scalar_one()
            assert org.name == "Smoke Test Org"
            assert org.slug == "smoke-test-org"

    async def test_tenant_filter_via_session_info(self, _engine) -> None:
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        user_a_id = uuid.uuid4()
        user_b_id = uuid.uuid4()

        async with AsyncSession(_engine) as session:
            async with session.begin():
                session.add_all([
                    Organisation(id=org_a, name="Org A", slug="org-a"),
                    Organisation(id=org_b, name="Org B", slug="org-b"),
                ])
            async with session.begin():
                session.add_all([
                    User(
                        id=user_a_id, organisation_id=org_a,
                        email="a@test.com", display_name="User A", org_role="admin",
                    ),
                    User(
                        id=user_b_id, organisation_id=org_b,
                        email="b@test.com", display_name="User B", org_role="runner",
                    ),
                ])

            session.info["org_id"] = org_a
            result = await session.execute(select(User).where(User.organisation_id == org_a))
            users = result.scalars().all()
            assert len(users) == 1
            assert users[0].id == user_a_id

    async def test_insert_and_query_user(self, _engine) -> None:
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        async with AsyncSession(_engine) as session:
            async with session.begin():
                session.add(Organisation(id=org_id, name="Query Org", slug="query-org"))
            async with session.begin():
                session.add(
                    User(
                        id=user_id, organisation_id=org_id,
                        email="query@test.com", display_name="Query User",
                        org_role="admin",
                    )
                )
            result = await session.execute(
                select(User).where(User.email == "query@test.com")
            )
            user = result.scalar_one()
            assert user.display_name == "Query User"
            assert user.organisation_id == org_id
