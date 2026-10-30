"""Unit tests for the System Maps discovery persistence adapter."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.repositories.system_map import _DISCOVERY_NAMESPACE, SystemMapRepository

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _session_returning(row: object | None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


async def test_get_or_create_draft_returns_existing_map() -> None:
    existing = MagicMock()
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
    assert created.content_json["stages"] == []
    assert created.content_json["edges"] == []
    session.add.assert_called_once_with(created)
    session.flush.assert_awaited_once()


async def test_save_discovery_revision_updates_map_and_stamps_revision_id() -> None:
    lifecycle_map = MagicMock()
    lifecycle_map.version = 1
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
    assert revision_id == uuid.uuid5(_DISCOVERY_NAMESPACE, f"{_MAP_ID}:abc123")
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
