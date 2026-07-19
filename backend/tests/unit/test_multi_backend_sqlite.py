"""Smoke test: SQLite backend with create_all, CRUD, and tenant filtering.

Verifies that core ORM operations work on the sqlite+aiosqlite driver
without any Postgres-specific features (RLS, advisory locks, etc.).
"""

import os
import uuid

import anyio
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_multi_backend.db")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("MODULO_DB", "sqlite")

import modulo.db.models  # noqa: F401
from modulo.db.models import Account
from modulo.db.models.base import Base
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation

_DB_URL = "sqlite+aiosqlite:///./test_multi_backend.db"
_DB_PATH = "./test_multi_backend.db"


@pytest.fixture(scope="module")
def _engine():
    engine = create_async_engine(_DB_URL, echo=False)
    yield engine


@pytest.fixture(scope="module")
async def _tables(_engine):
    # SQLite does not support ARRAY type - skip tables that use it
    from sqlalchemy import ARRAY

    tables_to_create = [t for t in Base.metadata.sorted_tables if not any(isinstance(c.type, ARRAY) for c in t.columns)]
    async with _engine.begin() as conn:
        await conn.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables_to_create))
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _engine.dispose()
    if await anyio.to_thread.run_sync(os.path.exists, _DB_PATH):
        await anyio.to_thread.run_sync(os.remove, _DB_PATH)


@pytest.fixture(autouse=True)
async def _clear_data(_engine, _tables):
    from sqlalchemy import ARRAY

    async with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if not any(isinstance(c.type, ARRAY) for c in table.columns):
                await conn.execute(table.delete())
    yield


class TestSqliteMultiBackend:
    async def test_create_and_retrieve_org(self, _engine) -> None:
        org_id = uuid.uuid4()
        async with AsyncSession(_engine) as session:
            async with session.begin():
                session.add(Organisation(id=org_id, name="Smoke Test Org", slug="smoke-test-org"))
            result = await session.execute(select(Organisation).where(Organisation.id == org_id))
            org = result.scalar_one()
            assert org.name == "Smoke Test Org"
            assert org.slug == "smoke-test-org"

    async def test_tenant_filter_via_org_membership(self, _engine) -> None:
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        user_a_id = uuid.uuid4()
        user_b_id = uuid.uuid4()

        async with AsyncSession(_engine) as session:
            async with session.begin():
                session.add_all(
                    [
                        Organisation(id=org_a, name="Org A", slug="org-a"),
                        Organisation(id=org_b, name="Org B", slug="org-b"),
                        Account(id=user_a_id, email="a@test.com", display_name="User A"),
                        Account(id=user_b_id, email="b@test.com", display_name="User B"),
                    ]
                )
            async with session.begin():
                session.add_all(
                    [
                        OrgMembership(account_id=user_a_id, organisation_id=org_a, role="admin"),
                        OrgMembership(account_id=user_b_id, organisation_id=org_b, role="runner"),
                    ]
                )

            session.info["org_id"] = org_a
            result = await session.execute(select(OrgMembership).where(OrgMembership.organisation_id == org_a))
            memberships = result.scalars().all()
            assert len(memberships) == 1
            assert memberships[0].account_id == user_a_id

    async def test_insert_and_query_account(self, _engine) -> None:
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        async with AsyncSession(_engine) as session:
            async with session.begin():
                session.add_all(
                    [
                        Organisation(id=org_id, name="Query Org", slug="query-org"),
                        Account(id=user_id, email="query@test.com", display_name="Query User"),
                    ]
                )
            async with session.begin():
                session.add(
                    OrgMembership(
                        account_id=user_id,
                        organisation_id=org_id,
                        role="admin",
                    )
                )
            result = await session.execute(select(Account).where(Account.email == "query@test.com"))
            user = result.scalar_one()
            assert user.display_name == "Query User"

            result = await session.execute(
                select(OrgMembership).where(
                    OrgMembership.account_id == user_id,
                    OrgMembership.organisation_id == org_id,
                )
            )
            membership = result.scalar_one()
            assert membership.role == "admin"
