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

    def test_apply_tenant_filter_adds_where_clause(self, repo: GenericRepository) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [{"entity": EntityWithOrg}]
        where_return = MagicMock(spec=Select)
        stmt.where.return_value = where_return

        result = repo.apply_tenant_filter(stmt, _ORG_ID)

        stmt.where.assert_called_once()
        assert result is where_return

    def test_apply_tenant_filter_adds_where_for_all_org_entities_in_join(self, repo: GenericRepository) -> None:
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

    def test_apply_tenant_filter_skips_entities_without_org_column(self, repo: GenericRepository) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [
            {"entity": EntityWithoutOrg},
            {"entity": EntityWithOrg},
        ]
        where_return = MagicMock(spec=Select)
        stmt.where.return_value = where_return

        result = repo.apply_tenant_filter(stmt, _ORG_ID)

        stmt.where.assert_called_once()
        assert result is where_return

    def test_apply_tenant_filter_skips_none_and_object_entities(self, repo: GenericRepository) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [
            {"entity": None},
            {"entity": object},
            {"entity": EntityWithOrg},
        ]
        where_return = MagicMock(spec=Select)
        stmt.where.return_value = where_return

        result = repo.apply_tenant_filter(stmt, _ORG_ID)

        stmt.where.assert_called_once()
        assert result is where_return

    def test_apply_tenant_filter_returns_stmt_unchanged_when_no_match(self, repo: GenericRepository) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [{"entity": EntityWithoutOrg}]

        result = repo.apply_tenant_filter(stmt, _ORG_ID)

        stmt.where.assert_not_called()
        assert result is stmt

    def test_apply_tenant_filter_returns_stmt_when_descriptions_empty(self, repo: GenericRepository) -> None:
        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = []

        result = repo.apply_tenant_filter(stmt, _ORG_ID)

        stmt.where.assert_not_called()
        assert result is stmt
