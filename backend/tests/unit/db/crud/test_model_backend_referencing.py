"""Unit tests for ``list_backends_referencing_fallback`` delete protection.

``fallback_backend_ids`` lives in a JSON column with no relational FK, so the
org-scoped reference scan is the enforcement point for deletion protection.
Uses a real in-memory SQLite database.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.model_backend import list_backends_referencing_fallback
from modulo.db.models.base import Base
from modulo.db.models.model_backend import ModelBackend

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[ModelBackend.__table__]))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_backend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID = _ORG_A,
    name: str,
    fallback_ids: list[str] | None = None,
) -> ModelBackend:
    backend = ModelBackend(
        organisation_id=org_id,
        name=name,
        display_name=name.title(),
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"encrypted",
        account_id=_ACCOUNT,
        fallback_backend_ids=fallback_ids,
    )
    session.add(backend)
    await session.flush()
    return backend


async def test_no_references_returns_empty(session: AsyncSession) -> None:
    target = await _seed_backend(session, name="fallback-target")
    await _seed_backend(session, name="standalone")
    assert not await list_backends_referencing_fallback(session, org_id=_ORG_A, backend_id=target.id)


async def test_referencing_backend_is_reported(session: AsyncSession) -> None:
    target = await _seed_backend(session, name="fallback-target")
    referencer = await _seed_backend(session, name="primary", fallback_ids=[str(target.id)])
    result = await list_backends_referencing_fallback(session, org_id=_ORG_A, backend_id=target.id)
    assert [mb.id for mb in result] == [referencer.id]


async def test_self_reference_is_not_reported_for_other_backend(session: AsyncSession) -> None:
    backend = await _seed_backend(session, name="self-ref", fallback_ids=[str(uuid.uuid4())])
    assert not await list_backends_referencing_fallback(session, org_id=_ORG_A, backend_id=backend.id)


async def test_other_org_references_are_ignored(session: AsyncSession) -> None:
    target = await _seed_backend(session, name="target-org-a")
    await _seed_backend(session, name="primary-org-b", org_id=_ORG_B, fallback_ids=[str(target.id)])
    assert not await list_backends_referencing_fallback(session, org_id=_ORG_A, backend_id=target.id)


async def test_multiple_referencers_all_reported(session: AsyncSession) -> None:
    target = await _seed_backend(session, name="target")
    await _seed_backend(session, name="first", fallback_ids=[str(target.id)])
    await _seed_backend(session, name="second", fallback_ids=[str(target.id)])
    result = await list_backends_referencing_fallback(session, org_id=_ORG_A, backend_id=target.id)
    assert sorted(mb.name for mb in result) == ["first", "second"]
