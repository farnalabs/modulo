"""Unit tests for db/repositories/generic.py — GenericRepository."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.repositories.generic import GenericRepository


class EntityWithOrg:
    organisation_id = None
    id = None
    name = None


class AnotherEntityWithOrg:
    organisation_id = None
    id = None
    title = None


class EntityWithoutOrg:
    id = None
    name = None


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestGenericRepository:
    @pytest.fixture()
    def repo(self) -> GenericRepository:
        return GenericRepository(session_factory=MagicMock())

    async def test_set_org_context_calls_set_rls_org(self, repo: GenericRepository) -> None:
        session = AsyncMock(spec=AsyncSession)
        with patch("modulo.db.repositories.base.set_rls_org") as mock_set_rls_org:
            await repo.set_org_context(session, _ORG_ID)
            mock_set_rls_org.assert_awaited_once_with(session, _ORG_ID)

    def test_apply_tenant_filter_adds_where_for_join(self, repo: GenericRepository) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [
            {"entity": EntityWithOrg},
            {"entity": AnotherEntityWithOrg},
        ]
        where_return = MagicMock(spec=Select)
        stmt.where.return_value = where_return

        result = repo.apply_tenant_filter(stmt, _ORG_ID)

        assert stmt.where.call_count == 1
        assert where_return.where.call_count == 1
        assert result is not stmt

    @pytest.mark.parametrize(
        ("column_descriptions", "where_call_count", "result_is_stmt"),
        [
            ([{"entity": EntityWithOrg}], 1, False),
            ([{"entity": EntityWithoutOrg}, {"entity": EntityWithOrg}], 1, False),
            ([{"entity": None}, {"entity": object}, {"entity": EntityWithOrg}], 1, False),
            ([{"entity": EntityWithoutOrg}], 0, True),
            ([], 0, True),
        ],
    )
    def test_apply_tenant_filter(
        self, repo: GenericRepository, column_descriptions: list, where_call_count: int, result_is_stmt: bool
    ) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = column_descriptions
        where_return = MagicMock(spec=Select)
        stmt.where.return_value = where_return

        result = repo.apply_tenant_filter(stmt, _ORG_ID)

        assert stmt.where.call_count == where_call_count
        if result_is_stmt:
            assert result is stmt
        else:
            assert result is where_return
