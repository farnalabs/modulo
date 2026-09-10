"""FAR-697 — copy_library_primitive FK regression against a real database.

Built-in modulo/community primitives live only in the in-memory seed cache:
they have no ``library_primitives`` row. The copy used to set
``forked_from`` to the source id anyway, and the FK on ``library_primitives.id``
rejected the insert — the MCP ``copy_library_primitive`` tool returned a
generic internal_error. These tests prove the fix against real Postgres with
real Alembic migrations and RLS.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.library_service import copy_to_adapt, get_primitive
from modulo.core.library_service._seed_data import _MODULO_BY_SLUG
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_PREFILL_CONSTRAINTS: dict[str, object] = {
    "source": "local",
    "primitive_type": "schema",
    "name": "FK Regression Source",
    "slug": "fk-regression-source",
    "description": None,
    "author": "tester",
    "version": "1.0",
    "tags": [],
    "content_json": {},
    "source_url": None,
    "forked_from": None,
    "checksum": None,
    "ed25519_signature": None,
    "verified": None,
    "download_count": None,
    "average_rating": None,
    "review_count": None,
}


@pytest_asyncio.fixture
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    from tests.integration.test_guardrail_config_api import _seed_org

    return await _seed_org(db_engine, "Lib-Copy-FK")


@pytest_asyncio.fixture
async def user(db_engine: AsyncEngine, org: uuid.UUID) -> AsyncGenerator[uuid.UUID, None]:
    from tests.integration.test_guardrail_config_api import _seed_user

    yield await _seed_user(db_engine, org, "lib-copy-fk@test.local")


async def test_builtin_copy_succeeds_with_null_forked_from(
    db_engine: AsyncEngine,
    org: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """Copying an in-memory builtin (Simplest Workflow) must not FK-fail."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        builtin = await get_primitive(session, org, _MODULO_BY_SLUG[("workflow", "simplest-workflow")].id)
        assert builtin is not None

    async with factory() as session:
        copied = await copy_to_adapt(session, org, builtin.id, created_by=user, via_mcp=False)

    assert copied.source == "local"
    assert copied.slug == "simplest-workflow-copy"
    assert copied.forked_from is None
    assert copied.account_id == user


async def test_db_source_copy_preserves_forked_from(
    db_engine: AsyncEngine,
    org: uuid.UUID,
    user: uuid.UUID,
) -> None:
    """A source that HAS a DB row keeps its forked_from link."""
    from modulo.db.crud.library_primitive import create_library_primitive

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org)
        source_row = await create_library_primitive(session, org_id=org, account_id=user, **_PREFILL_CONSTRAINTS)

    async with factory() as session:
        copied = await copy_to_adapt(session, org, source_row.id, created_by=user, via_mcp=False)

    assert copied.forked_from == source_row.id
    assert copied.slug == f"{source_row.slug}-copy"
