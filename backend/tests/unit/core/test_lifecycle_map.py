"""Unit tests for the org-scoped LifecycleMap CRUD service."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError

from modulo.core.lifecycle_map.service import (
    create_lifecycle_map,
    delete_lifecycle_map,
    get_lifecycle_map,
    list_lifecycle_maps,
    restore_lifecycle_map,
    update_lifecycle_map,
)
from modulo.db.crud.base import PageResult
from modulo.db.models.lifecycle_map import LifecycleMap

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


@pytest.fixture
def session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock()
    return session


def _make_map(**overrides: object) -> MagicMock:
    m = MagicMock(spec=LifecycleMap)
    m.id = overrides.get("id", _MAP_ID)
    m.organisation_id = overrides.get("organisation_id", _ORG_ID)
    m.name = overrides.get("name", "SDLC Workflow")
    m.description = overrides.get("description")
    m.owner_team_id = overrides.get("owner_team_id")
    m.visibility = overrides.get("visibility", "org")
    m.version = overrides.get("version", 1)
    m.content_json = overrides.get("content_json", {})
    m.archived_at = overrides.get("archived_at")
    m.account_id = overrides.get("account_id", _ACCOUNT_ID)
    m.deleted_at = overrides.get("deleted_at")
    return m


def _count_result(total: int) -> MagicMock:
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=total)
    return result


def _items_result(*items: MagicMock) -> MagicMock:
    return MagicMock(scalars=MagicMock(return_value=list(items)))


def _compiled_sql(statement: object) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------------------
# create_lifecycle_map
# ---------------------------------------------------------------------------


async def test_create_lifecycle_map_passes_all_fields(session: AsyncMock) -> None:
    created = _make_map()
    with patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=created) as model_cls:
        result = await create_lifecycle_map(
            session,
            org_id=_ORG_ID,
            name="Release Plan",
            account_id=_ACCOUNT_ID,
            description="Q3 release",
            owner_team_id=_TEAM_ID,
            visibility="team",
            version=3,
            content_json={"stages": []},
        )

    model_cls.assert_called_once_with(
        organisation_id=_ORG_ID,
        name="Release Plan",
        account_id=_ACCOUNT_ID,
        description="Q3 release",
        owner_team_id=_TEAM_ID,
        visibility="team",
        version=3,
        content_json={"stages": []},
    )
    session.add.assert_called_once_with(created)
    session.flush.assert_awaited_once()
    assert result is created


async def test_create_lifecycle_map_defaults(session: AsyncMock) -> None:
    with patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=_make_map()) as model_cls:
        await create_lifecycle_map(session, org_id=_ORG_ID, name="Default", account_id=_ACCOUNT_ID)

    _, kwargs = model_cls.call_args
    assert kwargs["description"] is None
    assert kwargs["owner_team_id"] is None
    assert kwargs["visibility"] == "org"
    assert kwargs["version"] == 1
    assert kwargs["content_json"] == {}


async def test_create_lifecycle_map_none_content_becomes_empty_dict(session: AsyncMock) -> None:
    with patch("modulo.core.lifecycle_map.service.LifecycleMap", return_value=_make_map()) as model_cls:
        await create_lifecycle_map(session, org_id=_ORG_ID, name="Empty", account_id=_ACCOUNT_ID, content_json=None)

    assert model_cls.call_args.kwargs["content_json"] == {}


# ---------------------------------------------------------------------------
# get_lifecycle_map
# ---------------------------------------------------------------------------


async def test_get_lifecycle_map_returns_found(session: AsyncMock) -> None:
    lm = _make_map()
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    result = await get_lifecycle_map(session, _MAP_ID)

    assert result is lm


async def test_get_lifecycle_map_returns_none_when_missing(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await get_lifecycle_map(session, _MAP_ID) is None


async def test_get_lifecycle_map_filters_archived_and_deleted(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    await get_lifecycle_map(session, _MAP_ID)

    statement = session.execute.await_args.args[0]
    sql = _compiled_sql(statement)
    assert "lifecycle_maps.id =" in sql
    assert _MAP_ID.hex in sql
    assert "archived_at IS NULL" in sql
    assert "deleted_at IS NULL" in sql


# ---------------------------------------------------------------------------
# list_lifecycle_maps
# ---------------------------------------------------------------------------


async def test_list_lifecycle_maps_returns_paginated_result(session: AsyncMock) -> None:
    lm_a, lm_b = _make_map(name="A"), _make_map(name="B")
    session.execute.side_effect = [_count_result(10), _items_result(lm_a, lm_b)]

    result = await list_lifecycle_maps(session, page=1, page_size=20)

    assert isinstance(result, PageResult)
    assert result.items == [lm_a, lm_b]
    assert result.total == 10
    assert result.page == 1
    assert result.page_size == 20


async def test_list_lifecycle_maps_applies_offset_and_limit(session: AsyncMock) -> None:
    session.execute.side_effect = [_count_result(50), _items_result()]

    await list_lifecycle_maps(session, page=3, page_size=10)

    items_statement = session.execute.await_args_list[1].args[0]
    sql = _compiled_sql(items_statement)
    assert "LIMIT 10" in sql
    assert "OFFSET 20" in sql


async def test_list_lifecycle_maps_excludes_archived_by_default(session: AsyncMock) -> None:
    session.execute.side_effect = [_count_result(1), _items_result(_make_map())]

    await list_lifecycle_maps(session, page=1, page_size=20)

    count_sql = _compiled_sql(session.execute.await_args_list[0].args[0])
    items_sql = _compiled_sql(session.execute.await_args_list[1].args[0])
    assert "archived_at IS NULL" in count_sql
    assert "archived_at IS NULL" in items_sql
    assert "deleted_at IS NULL" in count_sql


async def test_list_lifecycle_maps_includes_archived_when_requested(session: AsyncMock) -> None:
    session.execute.side_effect = [_count_result(1), _items_result(_make_map())]

    await list_lifecycle_maps(session, page=1, page_size=20, include_archived=True)

    count_sql = _compiled_sql(session.execute.await_args_list[0].args[0])
    assert "archived_at IS NULL" not in count_sql


async def test_list_lifecycle_maps_filters_by_owner_team(session: AsyncMock) -> None:
    session.execute.side_effect = [_count_result(1), _items_result(_make_map())]

    await list_lifecycle_maps(session, page=1, page_size=20, owner_team_id=_TEAM_ID)

    count_sql = _compiled_sql(session.execute.await_args_list[0].args[0])
    items_sql = _compiled_sql(session.execute.await_args_list[1].args[0])
    assert f"lifecycle_maps.owner_team_id = '{_TEAM_ID.hex}'" in count_sql
    assert f"lifecycle_maps.owner_team_id = '{_TEAM_ID.hex}'" in items_sql


async def test_list_lifecycle_maps_sorts_by_updated_at_desc(session: AsyncMock) -> None:
    session.execute.side_effect = [_count_result(1), _items_result(_make_map())]

    await list_lifecycle_maps(session, page=1, page_size=20)

    items_sql = _compiled_sql(session.execute.await_args_list[1].args[0])
    assert "ORDER BY lifecycle_maps.updated_at DESC" in items_sql


async def test_list_lifecycle_maps_returns_empty_page_on_programming_error(session: AsyncMock) -> None:
    session.execute.side_effect = [ProgrammingError("stmt", {}, Exception("no such table"))]

    result = await list_lifecycle_maps(session, page=1, page_size=20)

    assert isinstance(result, PageResult)
    assert result.items == []
    assert result.total == 0
    assert result.page == 1
    assert result.page_size == 20


# ---------------------------------------------------------------------------
# update_lifecycle_map
# ---------------------------------------------------------------------------


async def test_update_lifecycle_map_applies_updates_and_flushes(session: AsyncMock) -> None:
    lm = _make_map(name="Old Name", description="old")
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    result = await update_lifecycle_map(session, _MAP_ID, {"name": "New Name", "description": "new"})

    assert result is lm
    assert lm.name == "New Name"
    assert lm.description == "new"
    session.flush.assert_awaited_once()


async def test_update_lifecycle_map_returns_none_when_missing(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await update_lifecycle_map(session, _MAP_ID, {"name": "x"}) is None
    session.flush.assert_not_awaited()


async def test_update_lifecycle_map_skips_immutable_fields(session: AsyncMock) -> None:
    lm = _make_map()
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))
    new_org_id = uuid.uuid4()
    new_id = uuid.uuid4()

    result = await update_lifecycle_map(
        session,
        _MAP_ID,
        {
            "name": "Renamed",
            "id": new_id,
            "organisation_id": new_org_id,
            "deleted_at": _make_map().deleted_at,
            "created_at": "2020-01-01T00:00:00+00:00",
        },
    )

    assert result is lm
    assert lm.name == "Renamed"
    assert lm.id == _MAP_ID
    assert lm.organisation_id == _ORG_ID
    assert lm.deleted_at is None


# ---------------------------------------------------------------------------
# delete_lifecycle_map
# ---------------------------------------------------------------------------


async def test_delete_lifecycle_map_soft_deletes_and_returns_true(session: AsyncMock) -> None:
    lm = _make_map()
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    result = await delete_lifecycle_map(session, _MAP_ID)

    assert result is True
    assert lm.deleted_at is not None
    session.flush.assert_awaited_once()


async def test_delete_lifecycle_map_returns_false_when_missing(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await delete_lifecycle_map(session, _MAP_ID) is False
    session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# restore_lifecycle_map
# ---------------------------------------------------------------------------


async def test_restore_lifecycle_map_clears_deleted_at(session: AsyncMock) -> None:
    lm = _make_map(deleted_at=datetime.now(UTC))
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=lm))

    result = await restore_lifecycle_map(session, _MAP_ID)

    assert result is lm
    assert lm.deleted_at is None
    session.flush.assert_awaited_once()


async def test_restore_lifecycle_map_returns_none_when_missing(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    assert await restore_lifecycle_map(session, _MAP_ID) is None
    session.flush.assert_not_awaited()


async def test_restore_lifecycle_map_only_targets_deleted(session: AsyncMock) -> None:
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    await restore_lifecycle_map(session, _MAP_ID)

    statement = session.execute.await_args.args[0]
    sql = _compiled_sql(statement)
    assert "deleted_at IS NOT NULL" in sql
    assert _MAP_ID.hex in sql
