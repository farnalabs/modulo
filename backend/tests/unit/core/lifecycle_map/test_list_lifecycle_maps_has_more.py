"""``list_lifecycle_maps`` must report ``has_more`` on offset paging (FAR-1877).

``list_lifecycle_maps`` computes a real ``total`` via a count query and slices
with ``offset``/``limit``, but returned ``PageResult(...)`` without passing
``has_more`` — so it silently inherited the dataclass default ``False`` and a
page/page-size client could never detect that more pages exist. This drives the
REAL service function against in-memory SQLite so the count + offset arithmetic
is exercised end-to-end; the tests FAIL without the fix and PASS with it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.lifecycle_map.service import list_lifecycle_maps
from modulo.db.models.base import Base
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.organisation import Organisation

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

_PAGE_SIZE = 3
_COUNT = _PAGE_SIZE + 2  # 5 rows: page 1 is full with more beyond, page 2 is final.
_FINAL_PAGE = 2

_TABLES: list[Table] = [Organisation.__table__, LifecycleMap.__table__]


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        # lifecycle_maps carries FKs to teams/accounts that this fixture does not
        # create; inserts must not be constrained by them.
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        for i in range(_COUNT):
            s.add(
                LifecycleMap(
                    organisation_id=_ORG,
                    name=f"map {i}",
                    account_id=_ACCOUNT,
                    visibility="org",
                    version=1,
                    content_json={},
                )
            )
        await s.flush()
        yield s


async def test_first_page_reports_has_more(session: AsyncSession) -> None:
    page = await list_lifecycle_maps(session, page=1, page_size=_PAGE_SIZE)

    assert page.total == _COUNT
    assert len(page.items) == _PAGE_SIZE
    assert page.has_more is True


async def test_final_page_reports_no_more(session: AsyncSession) -> None:
    page = await list_lifecycle_maps(session, page=_FINAL_PAGE, page_size=_PAGE_SIZE)

    assert page.total == _COUNT
    assert len(page.items) == _COUNT - _PAGE_SIZE
    assert page.has_more is False


async def test_empty_result_reports_no_more(session: AsyncSession) -> None:
    page = await list_lifecycle_maps(session, page=1, page_size=_PAGE_SIZE, owner_team_id=uuid.uuid4())

    assert page.total == 0
    assert not page.items
    assert page.has_more is False
