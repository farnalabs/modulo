"""Unit tests for the System Maps discovery persistence adapter."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.system_map_persistence import (
    _DISCOVERY_MARKER,
    _DISCOVERY_NAMESPACE,
    SystemMapNameConflictError,
    SystemMapRepository,
)
from modulo.db.models.lifecycle_map import LifecycleMap

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _discovery_map(*, name: str = "Delivery") -> LifecycleMap:
    return LifecycleMap(
        organisation_id=_ORG_ID,
        name=name,
        account_id=_USER_ID,
        updated_by=_USER_ID,
        visibility="org",
        version=1,
        content_json={_DISCOVERY_MARKER: True, "stages": [], "edges": []},
    )


def _session_returning(row: object | None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


async def test_get_or_create_draft_returns_existing_discovery_map() -> None:
    existing = _discovery_map()
    session = _session_returning(existing)

    returned = await SystemMapRepository().get_or_create_draft(
        session,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        name="Delivery",
        description="draft",
    )

    assert returned is existing
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


async def test_get_or_create_draft_refuses_to_adopt_a_user_authored_map() -> None:
    user_map = LifecycleMap(
        organisation_id=_ORG_ID,
        name="Delivery",
        account_id=_USER_ID,
        visibility="org",
        version=1,
        content_json={"stages": [], "edges": []},
    )
    session = _session_returning(user_map)

    with pytest.raises(SystemMapNameConflictError, match="not a discovery draft"):
        await SystemMapRepository().get_or_create_draft(
            session,
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            name="Delivery",
            description="draft",
        )

    session.add.assert_not_called()
    session.flush.assert_not_awaited()


async def test_get_or_create_draft_creates_and_flushes_new_map() -> None:
    session = _session_returning(None)

    created = await SystemMapRepository().get_or_create_draft(
        session,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        name="Delivery",
        description="draft",
    )

    assert isinstance(created, LifecycleMap)
    assert created.organisation_id == _ORG_ID
    assert created.name == "Delivery"
    assert created.description == "draft"
    assert created.visibility == "org"
    assert created.version == 1
    assert created.content_json[_DISCOVERY_MARKER] is True
    assert not created.content_json["stages"]
    assert not created.content_json["edges"]
    session.add.assert_called_once_with(created)
    session.flush.assert_awaited_once()


async def test_save_discovery_revision_updates_map_and_stamps_revision_id() -> None:
    lifecycle_map = _discovery_map()
    session = _session_returning(lifecycle_map)
    content = {"discovery_fingerprint": "abc123"}

    returned, revision_id = await SystemMapRepository().save_discovery_revision(
        session,
        organisation_id=_ORG_ID,
        map_id=_MAP_ID,
        content=content,
        updated_by=_USER_ID,
    )

    assert returned is lifecycle_map
    assert lifecycle_map.version == 2
    assert lifecycle_map.updated_by == _USER_ID
    assert lifecycle_map.content_json["discovery_revision_id"] == str(revision_id)
    assert lifecycle_map.content_json[_DISCOVERY_MARKER] is True
    expected_revision_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{_MAP_ID}:abc123")
    assert revision_id == expected_revision_id
    session.flush.assert_awaited_once()


async def test_save_discovery_revision_raises_when_map_is_missing() -> None:
    session = _session_returning(None)

    with pytest.raises(ValueError, match="discovery draft map not found"):
        await SystemMapRepository().save_discovery_revision(
            session,
            organisation_id=_ORG_ID,
            map_id=_MAP_ID,
            content={"discovery_fingerprint": "abc123"},
            updated_by=_USER_ID,
        )

    session.flush.assert_not_awaited()


async def test_save_discovery_revision_rejects_another_organisation() -> None:
    lifecycle_map = _discovery_map()
    session = _session_returning(lifecycle_map)

    with pytest.raises(ValueError, match="discovery draft map not found"):
        await SystemMapRepository().save_discovery_revision(
            session,
            organisation_id=uuid.uuid4(),
            map_id=_MAP_ID,
            content={"discovery_fingerprint": "abc123"},
            updated_by=_USER_ID,
        )

    session.flush.assert_not_awaited()


async def test_save_discovery_revision_rejects_a_non_discovery_map() -> None:
    lifecycle_map = LifecycleMap(
        organisation_id=_ORG_ID,
        name="Delivery",
        account_id=_USER_ID,
        visibility="org",
        version=1,
        content_json={"stages": [], "edges": []},
    )
    session = _session_returning(lifecycle_map)

    with pytest.raises(ValueError, match="not a discovery draft"):
        await SystemMapRepository().save_discovery_revision(
            session,
            organisation_id=_ORG_ID,
            map_id=_MAP_ID,
            content={"discovery_fingerprint": "abc123"},
            updated_by=_USER_ID,
        )

    session.flush.assert_not_awaited()
