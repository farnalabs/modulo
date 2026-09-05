"""Unit tests for Schema/SchemaVersion CRUD (mocked session, no DB)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.crud.schema import SchemaDeletionProtectedError

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_FOLDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_schema(**overrides: object) -> MagicMock:
    schema = MagicMock()
    schema.id = _SCHEMA_ID
    schema.organisation_id = _ORG_ID
    schema.name = overrides.get("name", "metrics")
    schema.system = overrides.get("system", False)
    schema.deprecated = overrides.get("deprecated", False)
    return schema


def _count_result(value: int) -> MagicMock:
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=value)
    return result


def _exec_result(scalar_value: object = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)
    return result


class TestCreateSchema:
    async def test_creates_schema_with_org_id(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.schema.Schema", return_value=_make_schema()) as schema_cls:
            from modulo.db.crud.schema import create_schema

            result = await create_schema(
                mock_session,
                org_id=_ORG_ID,
                name="metrics",
                account_id=_ACCOUNT_ID,
                description="desc",
                abstract_name="Metrics",
            )
        kwargs = schema_cls.call_args.kwargs
        assert kwargs["organisation_id"] == _ORG_ID
        assert kwargs["account_id"] == _ACCOUNT_ID
        assert kwargs["description"] == "desc"
        assert kwargs["abstract_name"] == "Metrics"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        assert result is not None

    async def test_create_schema_minimal(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.schema.Schema", return_value=_make_schema()) as schema_cls:
            from modulo.db.crud.schema import create_schema

            await create_schema(mock_session, org_id=_ORG_ID, name="metrics", account_id=_ACCOUNT_ID)
        kwargs = schema_cls.call_args.kwargs
        assert kwargs["description"] is None
        assert kwargs["abstract_name"] is None


class TestGetSchema:
    async def test_returns_schema_when_found(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        mock_session.execute = AsyncMock(return_value=_exec_result(schema))
        from modulo.db.crud.schema import get_schema

        assert await get_schema(mock_session, _SCHEMA_ID) is schema

    async def test_returns_none_when_missing(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(None))
        from modulo.db.crud.schema import get_schema

        assert await get_schema(mock_session, _SCHEMA_ID) is None


class TestListSchemas:
    async def test_lists_with_has_more(self, mock_session: AsyncMock) -> None:
        schemas = [_make_schema() for _ in range(3)]
        count_mock = _count_result(3)
        listing = MagicMock()
        listing.scalars = MagicMock(return_value=list(schemas))
        mock_session.execute = AsyncMock(side_effect=[count_mock, listing])

        from modulo.db.crud.schema import list_schemas

        result = await list_schemas(mock_session, limit=2)
        assert isinstance(result, PageResult)
        assert result.total == 3
        assert len(result.items) == 2
        assert result.has_more is True
        assert result.next_cursor is None

    async def test_folder_filter_applies(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=[_count_result(0), MagicMock(scalars=MagicMock(return_value=[]))])
        from modulo.db.crud.schema import list_schemas

        result = await list_schemas(mock_session, folder_id=_FOLDER_ID)
        assert result.total == 0
        assert not result.items

    async def test_programming_error_returns_empty(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))
        from modulo.db.crud.schema import list_schemas

        result = await list_schemas(mock_session)
        assert isinstance(result, PageResult)
        assert result.total == 0
        assert not result.items

    async def test_cursor_uses_cursor_paginator(self, mock_session: AsyncMock) -> None:
        count_mock = _count_result(7)
        mock_session.execute = AsyncMock(side_effect=[count_mock])
        schemas = [_make_schema()]
        with (
            patch("modulo.db.crud.schema.CursorPaginator") as paginator_cls,
        ):
            paginate_result = MagicMock(
                items=schemas,
                next_cursor="abc",
                has_more=True,
            )
            paginator_cls.return_value.paginate = AsyncMock(return_value=paginate_result)
            from modulo.db.crud.schema import list_schemas

            result = await list_schemas(mock_session, cursor="abc", limit=5)
        paginator_cls.return_value.paginate.assert_awaited_once()
        assert result.total == 7
        assert result.items == schemas
        assert result.next_cursor == "abc"
        assert result.has_more is True


class TestUpdateAndDeprecate:
    async def test_update_schema(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        with (
            patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)),
            patch("modulo.db.crud.schema.apply_updates") as apply_updates,
        ):
            from modulo.db.crud.schema import update_schema

            result = await update_schema(mock_session, _SCHEMA_ID, {"name": "renamed"})
        apply_updates.assert_called_once_with(schema, {"name": "renamed"})
        assert result is schema

    async def test_update_schema_missing_returns_none(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=None)):
            from modulo.db.crud.schema import update_schema

            assert await update_schema(mock_session, _SCHEMA_ID, {}) is None

    async def test_deprecate_schema_sets_flag_and_timestamp(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import deprecate_schema

            result = await deprecate_schema(mock_session, _SCHEMA_ID)
        assert result.deprecated is True
        assert result.deprecated_at is not None
        mock_session.flush.assert_awaited_once()

    async def test_deprecate_missing_returns_none(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=None)):
            from modulo.db.crud.schema import deprecate_schema

            assert await deprecate_schema(mock_session, _SCHEMA_ID) is None


class TestDeleteSchema:
    async def test_returns_false_when_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=None)):
            from modulo.db.crud.schema import delete_schema

            assert await delete_schema(mock_session, _SCHEMA_ID) is False

    async def test_system_schema_raises_409(self, mock_session: AsyncMock) -> None:
        schema = _make_schema(system=True)
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import delete_schema

            with pytest.raises(HTTPException) as exc_info:
                await delete_schema(mock_session, _SCHEMA_ID)
        assert exc_info.value.status_code == 409

    async def test_agent_reference_blocks_deletion(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        mock_session.execute = AsyncMock(side_effect=[_count_result(2), _count_result(0), _count_result(0)])
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import delete_schema

            with pytest.raises(SchemaDeletionProtectedError) as exc_info:
                await delete_schema(mock_session, _SCHEMA_ID)
        assert "Agents" in str(exc_info.value)
        assert exc_info.value.schema_id == _SCHEMA_ID
        mock_session.delete.assert_not_awaited()

    async def test_snapshot_pin_reference_blocks_deletion(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        mock_session.execute = AsyncMock(
            side_effect=[_count_result(0), _count_result(2), _count_result(0)],
        )
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import delete_schema

            with pytest.raises(SchemaDeletionProtectedError) as exc_info:
                await delete_schema(mock_session, _SCHEMA_ID)
        assert "snapshot_schema_pins" in str(exc_info.value)

    async def test_library_reference_blocks_deletion(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        mock_session.execute = AsyncMock(side_effect=[_count_result(0), _count_result(0), _count_result(1)])
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import delete_schema

            with pytest.raises(SchemaDeletionProtectedError) as exc_info:
                await delete_schema(mock_session, _SCHEMA_ID)
        assert "LibraryPrimitives" in str(exc_info.value)

    def test_default_error_message_when_no_detail(self) -> None:
        error = SchemaDeletionProtectedError(_SCHEMA_ID)
        assert str(_SCHEMA_ID) in str(error)
        assert error.schema_id == _SCHEMA_ID

    async def test_force_skips_checks_and_deletes(self, mock_session: AsyncMock) -> None:
        schema = _make_schema(system=True)
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import delete_schema

            result = await delete_schema(mock_session, _SCHEMA_ID, force=True)
        assert result is True
        mock_session.delete.assert_awaited_once_with(schema)
        mock_session.flush.assert_awaited_once()

    async def test_combined_references_listed_in_detail(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        mock_session.execute = AsyncMock(side_effect=[_count_result(1), _count_result(1), _count_result(1)])
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import delete_schema

            with pytest.raises(SchemaDeletionProtectedError) as exc_info:
                await delete_schema(mock_session, _SCHEMA_ID)
        detail = str(exc_info.value)
        assert "Agents" in detail
        assert "snapshot_schema_pins" in detail
        assert "LibraryPrimitives" in detail

    async def test_successful_delete_without_references(self, mock_session: AsyncMock) -> None:
        schema = _make_schema()
        mock_session.execute = AsyncMock(side_effect=[_count_result(0), _count_result(0), _count_result(0)])
        with patch("modulo.db.crud.schema.get_schema", AsyncMock(return_value=schema)):
            from modulo.db.crud.schema import delete_schema

            result = await delete_schema(mock_session, _SCHEMA_ID)
        assert result is True
        mock_session.delete.assert_awaited_once_with(schema)


class TestSchemaVersions:
    async def test_create_schema_version(self, mock_session: AsyncMock) -> None:
        created = MagicMock()
        created.organisation_id = _ORG_ID
        with patch("modulo.db.crud.schema.SchemaVersion", return_value=created) as sv_cls:
            from modulo.db.crud.schema import create_schema_version

            result = await create_schema_version(
                mock_session,
                org_id=_ORG_ID,
                schema_id=_SCHEMA_ID,
                version="1.0.0",
                version_number=1,
                definition_json={"type": "object"},
                account_id=_ACCOUNT_ID,
            )
        kwargs = sv_cls.call_args.kwargs
        assert kwargs["organisation_id"] == _ORG_ID
        assert kwargs["schema_id"] == _SCHEMA_ID
        assert kwargs["definition_json"] == {"type": "object"}
        assert kwargs["published"] is False
        mock_session.add.assert_called_once()
        assert result is created

    async def test_get_schema_version_found_and_missing(self, mock_session: AsyncMock) -> None:
        sv = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[_exec_result(sv), _exec_result(None)])
        from modulo.db.crud.schema import get_schema_version

        assert await get_schema_version(mock_session, _SCHEMA_ID, "1.0.0") is sv
        assert await get_schema_version(mock_session, _SCHEMA_ID, "9.9.9") is None

    async def test_list_schema_versions(self, mock_session: AsyncMock) -> None:
        versions = [MagicMock(), MagicMock()]
        count_mock = _count_result(2)
        listing = MagicMock(scalars=MagicMock(return_value=list(versions)))
        mock_session.execute = AsyncMock(side_effect=[count_mock, listing])
        from modulo.db.crud.schema import list_schema_versions

        result = await list_schema_versions(mock_session, _SCHEMA_ID, page=2, page_size=10)
        assert result.total == 2
        assert result.items == versions
        assert result.page == 2

    async def test_list_schema_versions_programming_error(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("boom")))
        from modulo.db.crud.schema import list_schema_versions

        result = await list_schema_versions(mock_session, _SCHEMA_ID)
        assert result.total == 0
        assert not result.items
