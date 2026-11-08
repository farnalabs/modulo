"""Unit tests for ConnectorInstance CRUD operations (mocked session, no DB)."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_connector_instance(**overrides: Any) -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
    ci.organisation_id = _ORG_ID
    ci.name = overrides.get("name", "test-connector")
    ci.connector_type_id = overrides.get("connector_type_id", "github")
    ci.account_id = _ACCOUNT_ID
    ci.credentials_ciphertext = overrides.get("credentials_ciphertext", b"secret")
    ci.config_json = overrides.get("config_json", {})
    ci.allowed_operations = overrides.get("allowed_operations", [])
    ci.visibility = overrides.get("visibility", "org")
    ci.owner_team_id = overrides.get("owner_team_id")
    ci.tier = overrides.get("tier", "native")
    ci.status = overrides.get("status", "active")
    ci.degraded_at = overrides.get("degraded_at")
    ci.last_skip_error = overrides.get("last_skip_error")
    ci.__table__ = MagicMock()
    return ci


def _exec_result(scalar_value: Any = None, scalars_list: list[Any] | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)
    result.scalar_one = MagicMock(return_value=1)
    result.scalar = MagicMock(return_value=scalar_value)
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=scalars_list or [])
    result.scalars = MagicMock(return_value=scalars)
    return result


class TestSanitizeSkipSummary:
    def test_strips_nul_and_truncates(self) -> None:
        from modulo.db.crud.connector_instance import _sanitize_skip_summary

        # NUL bytes should be stripped
        assert _sanitize_skip_summary("hello\x00world") == "helloworld"
        # Should truncate to 2000 chars
        long_str = "x" * 2005
        assert len(_sanitize_skip_summary(long_str)) == 2000
        # Should handle both NUL and truncation
        long_with_nul = "x" * 1000 + "\x00" + "y" * 1005
        result = _sanitize_skip_summary(long_with_nul)
        assert "\x00" not in result
        assert len(result) == 2000
        # Empty string
        assert not _sanitize_skip_summary("")
        # No NUL, no truncation needed
        assert _sanitize_skip_summary("normal string") == "normal string"


class TestCreateConnectorInstance:
    async def test_creates_with_all_fields(self, mock_session: AsyncMock) -> None:
        with patch(
            "modulo.db.crud.connector_instance.ConnectorInstance", return_value=_make_connector_instance()
        ) as mock_ci_cls:
            from modulo.db.crud.connector_instance import create_connector_instance

            result = await create_connector_instance(
                mock_session,
                org_id=_ORG_ID,
                name="test-connector",
                connector_type_id="github",
                account_id=_ACCOUNT_ID,
                credentials_ciphertext=b"secret",
                config_json={"key": "value"},
                allowed_operations=["read", "write"],
                visibility="team",
                owner_team_id=_TEAM_ID,
                tier="preview",
            )

        kwargs = mock_ci_cls.call_args.kwargs
        assert kwargs["organisation_id"] == _ORG_ID
        assert kwargs["name"] == "test-connector"
        assert kwargs["connector_type_id"] == "github"
        assert kwargs["account_id"] == _ACCOUNT_ID
        assert kwargs["credentials_ciphertext"] == b"secret"
        assert kwargs["config_json"] == {"key": "value"}
        assert kwargs["allowed_operations"] == ["read", "write"]
        assert kwargs["visibility"] == "team"
        assert kwargs["owner_team_id"] == _TEAM_ID
        assert kwargs["tier"] == "preview"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        assert result is not None

    async def test_creates_with_defaults(self, mock_session: AsyncMock) -> None:
        with patch(
            "modulo.db.crud.connector_instance.ConnectorInstance", return_value=_make_connector_instance()
        ) as mock_ci_cls:
            from modulo.db.crud.connector_instance import create_connector_instance

            await create_connector_instance(
                mock_session,
                org_id=_ORG_ID,
                name="test",
                connector_type_id="github",
                account_id=_ACCOUNT_ID,
                credentials_ciphertext=b"secret",
            )

        kwargs = mock_ci_cls.call_args.kwargs
        assert not kwargs["config_json"]
        assert not kwargs["allowed_operations"]
        assert kwargs["visibility"] == "org"
        assert kwargs["owner_team_id"] is None
        assert kwargs["tier"] == "native"


class TestGetConnectorInstance:
    async def test_returns_instance_when_found(self, mock_session: AsyncMock) -> None:
        ci = _make_connector_instance()
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=ci))
        from modulo.db.crud.connector_instance import get_connector_instance

        result = await get_connector_instance(mock_session, _CONNECTOR_ID)
        assert result is ci

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=None))
        from modulo.db.crud.connector_instance import get_connector_instance

        result = await get_connector_instance(mock_session, _CONNECTOR_ID)
        assert result is None


class TestListConnectorInstances:
    async def test_returns_paginated_with_offset(self, mock_session: AsyncMock) -> None:
        instances = [_make_connector_instance(), _make_connector_instance(name="other")]
        count_result = _exec_result()
        count_result.scalar_one = MagicMock(return_value=5)
        listing_result = MagicMock()
        listing_result.scalars = MagicMock(return_value=list(instances))
        mock_session.execute = AsyncMock(side_effect=[count_result, listing_result])

        from modulo.db.crud.connector_instance import list_connector_instances

        result = await list_connector_instances(mock_session, organisation_id=_ORG_ID, page=2, page_size=2)
        assert isinstance(result, PageResult)
        assert result.total == 5
        assert result.page == 2
        assert result.page_size == 2
        assert result.items == instances
        assert result.has_more is True

    async def test_uses_cursor_pagination_when_cursor_provided(self, mock_session: AsyncMock) -> None:
        # Import first, then patch
        from modulo.db.crud.connector_instance import list_connector_instances
        from modulo.db.crud.pagination import CursorPaginator

        instances = [_make_connector_instance()]
        paginator_mock = AsyncMock()
        cp_result = MagicMock()
        cp_result.items = instances
        cp_result.total = 10
        cp_result.next_cursor = "next-cursor"
        cp_result.has_more = True
        paginator_mock.paginate = AsyncMock(return_value=cp_result)

        with patch.object(CursorPaginator, "__new__", return_value=paginator_mock):
            result = await list_connector_instances(
                mock_session,
                organisation_id=_ORG_ID,
                cursor="test-cursor",
                page_size=5,
            )

        assert isinstance(result, PageResult)
        assert result.items == instances
        assert result.total == 10
        assert result.next_cursor == "next-cursor"
        assert result.has_more is True
        paginator_mock.paginate.assert_awaited_once()

    async def test_cursor_pagination_applies_excluded_tiers(self, mock_session: AsyncMock) -> None:
        from modulo.db.crud.connector_instance import list_connector_instances
        from modulo.db.crud.pagination import CursorPaginator
        from tests.unit.crud.helpers import compile_sql

        instances = [_make_connector_instance()]
        paginator_mock = AsyncMock()
        cp_result = MagicMock()
        cp_result.items = instances
        cp_result.total = 10
        cp_result.next_cursor = "next-cursor"
        cp_result.has_more = False
        paginator_mock.paginate = AsyncMock(return_value=cp_result)

        with patch.object(CursorPaginator, "__new__", return_value=paginator_mock):
            await list_connector_instances(
                mock_session,
                organisation_id=_ORG_ID,
                cursor="test-cursor",
                excluded_tiers=["in_dev", "preview"],
            )

        stmt = paginator_mock.paginate.await_args.args[1]
        sql = compile_sql(stmt)
        assert "tier NOT IN ('in_dev', 'preview')" in sql

    async def test_cursor_pagination_applies_org_filter(self, mock_session: AsyncMock) -> None:
        """The cursor path must carry the org predicate into the paginated stmt."""
        from modulo.db.crud.connector_instance import list_connector_instances
        from modulo.db.crud.pagination import CursorPaginator
        from tests.unit.crud.helpers import compile_sql

        instances = [_make_connector_instance()]
        paginator_mock = AsyncMock()
        cp_result = MagicMock()
        cp_result.items = instances
        cp_result.total = 10
        cp_result.next_cursor = "next-cursor"
        cp_result.has_more = False
        paginator_mock.paginate = AsyncMock(return_value=cp_result)

        with patch.object(CursorPaginator, "__new__", return_value=paginator_mock):
            await list_connector_instances(
                mock_session,
                organisation_id=_ORG_ID,
                cursor="test-cursor",
            )

        stmt = paginator_mock.paginate.await_args.args[1]
        sql = compile_sql(stmt)
        assert f"organisation_id = '{_ORG_ID.hex}'" in sql

    async def test_cursor_pagination_without_organisation_id(self, mock_session: AsyncMock) -> None:
        """With no org supplied the cursor path skips the org predicate but keeps the tier filter."""
        from modulo.db.crud.connector_instance import list_connector_instances
        from modulo.db.crud.pagination import CursorPaginator
        from tests.unit.crud.helpers import compile_sql, where_column_names

        paginator_mock = AsyncMock()
        cp_result = MagicMock()
        cp_result.items = []
        cp_result.total = 0
        cp_result.next_cursor = None
        cp_result.has_more = False
        paginator_mock.paginate = AsyncMock(return_value=cp_result)

        with patch.object(CursorPaginator, "__new__", return_value=paginator_mock):
            await list_connector_instances(mock_session, cursor="test-cursor")

        stmt = paginator_mock.paginate.await_args.args[1]
        assert "organisation_id" not in where_column_names(stmt)
        assert "tier NOT IN ('in_dev')" in compile_sql(stmt)

    async def test_offset_pagination_applies_excluded_tiers(self, mock_session: AsyncMock) -> None:
        from tests.unit.crud.helpers import compile_sql

        instances = [_make_connector_instance()]
        count_result = _exec_result()
        count_result.scalar_one = MagicMock(return_value=5)
        listing_result = MagicMock()
        listing_result.scalars = MagicMock(return_value=list(instances))
        mock_session.execute = AsyncMock(side_effect=[count_result, listing_result])

        from modulo.db.crud.connector_instance import list_connector_instances

        await list_connector_instances(
            mock_session,
            organisation_id=_ORG_ID,
            excluded_tiers=["in_dev"],
        )

        assert mock_session.execute.call_count == 2
        for call in mock_session.execute.await_args_list:
            assert "tier NOT IN ('in_dev')" in compile_sql(call.args[0])

    async def test_works_without_organisation_id(self, mock_session: AsyncMock) -> None:
        instances = [_make_connector_instance()]
        count_result = _exec_result()
        count_result.scalar_one = MagicMock(return_value=3)
        listing_result = MagicMock()
        listing_result.scalars = MagicMock(return_value=list(instances))
        mock_session.execute = AsyncMock(side_effect=[count_result, listing_result])

        from modulo.db.crud.connector_instance import list_connector_instances

        result = await list_connector_instances(mock_session, page=1, page_size=20)
        assert isinstance(result, PageResult)
        assert result.total == 3
        assert result.items == instances


class TestUpdateConnectorInstance:
    async def test_applies_updates_and_flushes(self, mock_session: AsyncMock) -> None:
        ci = _make_connector_instance()
        with (
            patch("modulo.db.crud.connector_instance.get_connector_instance", AsyncMock(return_value=ci)),
            patch("modulo.db.crud.connector_instance.apply_updates") as apply_updates,
        ):
            from modulo.db.crud.connector_instance import update_connector_instance

            result = await update_connector_instance(
                mock_session, _CONNECTOR_ID, {"name": "updated", "config_json": {"new": "value"}}
            )

        apply_updates.assert_called_once_with(ci, {"name": "updated", "config_json": {"new": "value"}})
        mock_session.flush.assert_awaited_once()
        assert result is ci

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.connector_instance.get_connector_instance", AsyncMock(return_value=None)):
            from modulo.db.crud.connector_instance import update_connector_instance

            result = await update_connector_instance(mock_session, _CONNECTOR_ID, {"name": "updated"})
        assert result is None


class TestMarkInstancesDegraded:
    async def test_executes_batched_update(self, mock_session: AsyncMock) -> None:
        instance_ids = {uuid.uuid4(), uuid.uuid4()}
        skipped = {iid: f"error for {iid}" for iid in instance_ids}

        from modulo.db.crud.connector_instance import mark_instances_degraded

        await mark_instances_degraded(mock_session, skipped)

        mock_session.execute.assert_awaited_once()
        mock_session.flush.assert_awaited_once()

    async def test_sanitizes_summaries(self, mock_session: AsyncMock) -> None:
        """NUL bytes in summaries must be stripped from the batched UPDATE."""
        from tests.unit.crud.helpers import compile_sql

        bad_summary = "error\x00with\x00nuls"
        instance_id = uuid.uuid4()
        skipped = {instance_id: bad_summary}

        from modulo.db.crud.connector_instance import mark_instances_degraded

        await mark_instances_degraded(mock_session, skipped)

        sql = compile_sql(mock_session.execute.await_args.args[0])
        assert "errorwithnuls" in sql
        assert "\x00" not in sql


class TestClearDegradedMarkers:
    async def test_executes_batched_update(self, mock_session: AsyncMock) -> None:
        instance_ids = [uuid.uuid4(), uuid.uuid4()]

        from modulo.db.crud.connector_instance import clear_degraded_markers

        await clear_degraded_markers(mock_session, instance_ids)

        mock_session.execute.assert_awaited_once()
        mock_session.flush.assert_awaited_once()

    async def test_accepts_any_collection(self, mock_session: AsyncMock) -> None:
        """Should accept any Collection[UUID], not just list."""
        from modulo.db.crud.connector_instance import clear_degraded_markers

        instance_ids = {uuid.uuid4(), uuid.uuid4()}  # set
        await clear_degraded_markers(mock_session, instance_ids)
        mock_session.execute.assert_awaited_once()
        mock_session.flush.assert_awaited_once()


class TestDeleteConnectorInstance:
    async def test_returns_true_and_deletes(self, mock_session: AsyncMock) -> None:
        ci = _make_connector_instance()
        with patch("modulo.db.crud.connector_instance.get_connector_instance", AsyncMock(return_value=ci)):
            from modulo.db.crud.connector_instance import delete_connector_instance

            result = await delete_connector_instance(mock_session, _CONNECTOR_ID)

        assert result is True
        mock_session.delete.assert_awaited_once_with(ci)
        mock_session.flush.assert_awaited_once()

    async def test_returns_false_when_not_found(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.connector_instance.get_connector_instance", AsyncMock(return_value=None)):
            from modulo.db.crud.connector_instance import delete_connector_instance

            result = await delete_connector_instance(mock_session, _CONNECTOR_ID)
        assert result is False
        mock_session.delete.assert_not_awaited()
        mock_session.flush.assert_not_awaited()
