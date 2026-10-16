"""Offset/page paging of ``list_pipelines`` must report ``has_more``.

The non-cursor branch used to return ``PageResult(...)`` without passing
``has_more``, so it silently inherited the dataclass default ``False`` and a
page/page-size client could never detect that more pages exist (``total: 39``,
``has_more: false`` on every page). These tests drive the REAL CRUD against
in-memory SQLite so the count + offset arithmetic is exercised end-to-end, and
they FAIL without the fix.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Table
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.pipeline import list_pipelines
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

_TABLES: list[Table] = [Organisation.__table__, Pipeline.__table__]

_PIPELINE_COUNT = 5


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
        s.add(Organisation(id=_ORG, name="org", slug="org"))
        await s.flush()
        for i in range(_PIPELINE_COUNT):
            s.add(
                Pipeline(
                    organisation_id=_ORG,
                    name=f"pipeline {i}",
                    account_id=_ORG,
                    visibility="org",
                )
            )
        await s.flush()
        yield s


async def test_first_page_reports_has_more(session: AsyncSession) -> None:
    page = await list_pipelines(session, page=1, page_size=2, organisation_id=_ORG)

    assert page.total == _PIPELINE_COUNT
    assert len(page.items) == 2
    assert page.has_more is True


async def test_middle_page_reports_has_more(session: AsyncSession) -> None:
    page = await list_pipelines(session, page=2, page_size=2, organisation_id=_ORG)

    assert len(page.items) == 2
    # offset(2) + 2 items = 4 < 5 total -> one row still beyond this page.
    assert page.has_more is True


async def test_last_page_reports_no_more(session: AsyncSession) -> None:
    page = await list_pipelines(session, page=3, page_size=2, organisation_id=_ORG)

    assert len(page.items) == 1
    assert page.has_more is False


async def test_page_sized_to_total_reports_no_more(session: AsyncSession) -> None:
    page = await list_pipelines(session, page=1, page_size=_PIPELINE_COUNT, organisation_id=_ORG)

    assert page.total == _PIPELINE_COUNT
    assert page.has_more is False


async def test_page_beyond_end_reports_no_more(session: AsyncSession) -> None:
    page = await list_pipelines(session, page=4, page_size=2, organisation_id=_ORG)

    assert page.total == _PIPELINE_COUNT
    assert not page.items
    assert page.has_more is False


async def test_empty_result_reports_no_more(session: AsyncSession) -> None:
    page = await list_pipelines(session, page=1, page_size=10, organisation_id=uuid.uuid4())

    assert page.total == 0
    assert not page.items
    assert page.has_more is False


async def test_count_failure_returns_empty_page_with_has_more_false() -> None:
    """The ProgrammingError fallback reports total=0, so has_more must be False."""
    db_session = AsyncMock()
    db_session.execute = AsyncMock(side_effect=ProgrammingError("table missing", {}, None))

    page = await list_pipelines(db_session, page=1, page_size=20)

    assert page.total == 0
    assert not page.items
    assert page.has_more is False
