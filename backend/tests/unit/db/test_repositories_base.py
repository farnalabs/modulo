"""Unit tests for db/repositories/base.py — BaseRepository, extract_orm_entity."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select, column
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.base import BaseRepository, extract_orm_entity


class EntityWithOrg:
    organisation_id: uuid.UUID
    id: uuid.UUID
    name: str


class EntityWithoutOrg:
    id: uuid.UUID
    name: str


class _ConcreteRepo(BaseRepository):
    async def set_org_context(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        pass

    def apply_tenant_filter(self, stmt: Select, org_id: uuid.UUID) -> Select:
        return stmt


class TestExtractOrmEntity:
    def test_returns_entity_from_column_descriptions(self) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [{"entity": EntityWithOrg}]
        assert extract_orm_entity(stmt) is EntityWithOrg

    def test_returns_none_when_no_entity(self) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [{"entity": None}]
        assert extract_orm_entity(stmt) is None

    def test_skips_none_and_returns_subsequent_entity(self) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [{"entity": None}, {"entity": EntityWithOrg}]
        assert extract_orm_entity(stmt) is EntityWithOrg

    def test_returns_first_entity_when_multiple(self) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [
            {"entity": EntityWithoutOrg},
            {"entity": EntityWithOrg},
        ]
        assert extract_orm_entity(stmt) is EntityWithoutOrg

    def test_empty_descriptions_returns_none(self) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = []
        assert extract_orm_entity(stmt) is None


class TestBaseRepository:
    _ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

    @pytest.fixture()
    def repo(self) -> _ConcreteRepo:
        return _ConcreteRepo(session_factory=MagicMock())

    async def test_paginate_returns_page_result(self, repo: _ConcreteRepo) -> None:
        session = AsyncMock(spec=AsyncSession)
        stmt = MagicMock(spec=Select)

        count_result = MagicMock()
        count_result.scalar_one_or_none.return_value = 42

        scalars_result = MagicMock()
        scalars_result.all.return_value = ["item1", "item2"]
        items_result = MagicMock()
        items_result.scalars.return_value = scalars_result

        session.execute = AsyncMock()
        session.execute.side_effect = [count_result, items_result]

        # Give stmt.order_by(None).subquery() a real subquery that SA can use as FROM clause
        real_subquery = sa_select(column("x")).subquery()
        stmt.order_by.return_value.subquery.return_value = real_subquery

        result = await repo.paginate(session, stmt, page=2, page_size=10)

        assert result.total == 42
        assert result.items == ["item1", "item2"]
        assert result.page == 2
        assert result.page_size == 10
        stmt.offset.assert_called_once_with(10)
        stmt.offset.return_value.limit.assert_called_once_with(10)

    async def test_paginate_raises_for_page_zero(self, repo: _ConcreteRepo) -> None:
        with pytest.raises(ValueError, match="page must be >= 1"):
            await repo.paginate(AsyncMock(spec=AsyncSession), MagicMock(spec=Select), page=0, page_size=20)

    async def test_paginate_raises_for_page_size_zero(self, repo: _ConcreteRepo) -> None:
        with pytest.raises(ValueError, match="page_size must be >= 1"):
            await repo.paginate(AsyncMock(spec=AsyncSession), MagicMock(spec=Select), page=1, page_size=0)

    async def test_paginate_raises_for_negative_page(self, repo: _ConcreteRepo) -> None:
        with pytest.raises(ValueError, match="page must be >= 1"):
            await repo.paginate(AsyncMock(spec=AsyncSession), MagicMock(spec=Select), page=-1, page_size=20)

    async def test_execute_proxies_to_session(self, repo: _ConcreteRepo) -> None:
        session = AsyncMock(spec=AsyncSession)
        stmt = MagicMock(spec=Select)
        expected = MagicMock()
        session.execute = AsyncMock(return_value=expected)

        result = await repo.execute(session, stmt)

        session.execute.assert_awaited_once_with(stmt)
        assert result is expected
