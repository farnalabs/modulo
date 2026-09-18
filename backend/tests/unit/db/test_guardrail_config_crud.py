"""Unit tests for db/crud/guardrail_config.py (in-memory SQLite).

Exercises the REAL CRUD path (no mocks of the functions under test) against the
real ``Organisation`` model: get/set round-trip of ``guardrail_pins_json``,
overwrite semantics, and the ``NoResultFound`` path when the org row does not
exist (the update matches zero rows).
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import Table
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.guardrail_config import get_guardrail_pin, set_guardrail_pin
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_MISSING_ORG = uuid.UUID("00000000-0000-0000-0000-00000000ffff")

_TABLES: list[Table] = cast("list[Table]", [Organisation.__table__])

_PIN: dict[str, Any] = {
    "applied_hash": "a" * 64,
    "applied_at": "2026-08-15T00:00:00+00:00",
    "serialized_snapshot": "version: 1\nguardrails: []\n",
    "proposed_hash": None,
    "proposed_at": None,
    "serialized_proposal": None,
    "status": "clean",
}


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_org(session: AsyncSession, org_id: uuid.UUID = _ORG) -> None:
    session.add(Organisation(id=org_id, name="test org", slug=f"test-org-{org_id.hex[:8]}"))
    await session.flush()


async def test_get_returns_none_when_not_set(session: AsyncSession) -> None:
    await _seed_org(session)
    assert await get_guardrail_pin(session, _ORG) is None


async def test_set_then_get_round_trip(session: AsyncSession) -> None:
    await _seed_org(session)
    await set_guardrail_pin(session, _ORG, _PIN)
    await session.commit()
    stored = await get_guardrail_pin(session, _ORG)
    assert stored == _PIN


async def test_set_overwrites_previous_pin(session: AsyncSession) -> None:
    await _seed_org(session)
    await set_guardrail_pin(session, _ORG, _PIN)
    updated = dict(_PIN, status="drift", proposed_hash="b" * 64)
    await set_guardrail_pin(session, _ORG, updated)
    await session.commit()
    assert await get_guardrail_pin(session, _ORG) == updated


async def test_set_raises_no_result_found_when_org_missing(session: AsyncSession) -> None:
    with pytest.raises(NoResultFound):
        await set_guardrail_pin(session, _MISSING_ORG, _PIN)


async def test_set_does_not_mutate_other_org_pin(session: AsyncSession) -> None:
    other_org = uuid.uuid4()
    await _seed_org(session)
    await _seed_org(session, other_org)
    await set_guardrail_pin(session, _ORG, _PIN)
    await session.commit()
    assert await get_guardrail_pin(session, other_org) is None
