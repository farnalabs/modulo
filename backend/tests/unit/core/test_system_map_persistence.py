"""Unit tests for deterministic System Maps discovery persistence."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from modulo.core.system_map_persistence import (
    _DISCOVERY_NAMESPACE,
    SystemMapDiscoveryRepository,
)
from modulo.db.models.system_map import SystemMap

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


class _Result:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


async def test_save_discovery_revision_passes_deterministic_id_to_repository() -> None:
    system_map = SystemMap(organisation_id=_ORG_ID, name="Delivery", account_id=_USER_ID, id=_MAP_ID)
    repository = AsyncMock()
    repository.get_map_by_id.return_value = system_map
    repository.list_revisions.return_value = []
    created_revision = type("Revision", (), {"id": uuid.uuid4(), "revision_number": 1})()
    repository.create_revision.return_value = created_revision
    session = AsyncMock()
    session.execute.return_value = _Result(None)

    content = {"discovery_fingerprint": "fingerprint-1", "nodes": [], "relationships": []}
    saved_map, revision_id, revision_number = await SystemMapDiscoveryRepository(repository).save_discovery_revision(
        session,
        organisation_id=_ORG_ID,
        map_id=_MAP_ID,
        content=content,
        updated_by=_USER_ID,
    )

    expected_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{_MAP_ID}:fingerprint-1")
    assert saved_map is system_map
    assert revision_id == expected_id
    assert revision_number == 1
    repository.create_revision.assert_awaited_once_with(
        session,
        organisation_id=_ORG_ID,
        system_map_id=_MAP_ID,
        revision_number=1,
        revision_id=expected_id,
        content_json=content,
    )


async def test_save_identical_discovery_reuses_existing_revision_without_creating() -> None:
    revision_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{_MAP_ID}:fingerprint-1")
    existing_revision = type("Revision", (), {"id": revision_id, "revision_number": 7})()
    system_map = SystemMap(organisation_id=_ORG_ID, name="Delivery", account_id=_USER_ID, id=_MAP_ID)
    repository = AsyncMock()
    repository.get_map_by_id.return_value = system_map
    repository.list_revisions.return_value = [existing_revision]
    session = AsyncMock()
    session.execute.return_value = _Result(None)

    saved_map, returned_id, revision_number = await SystemMapDiscoveryRepository(repository).save_discovery_revision(
        session,
        organisation_id=_ORG_ID,
        map_id=_MAP_ID,
        content={"discovery_fingerprint": "fingerprint-1", "nodes": [], "relationships": []},
        updated_by=_USER_ID,
    )

    assert saved_map is system_map
    assert returned_id == revision_id
    assert revision_number == 7
    repository.create_revision.assert_not_awaited()


async def test_save_changed_discovery_creates_next_revision() -> None:
    existing_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{_MAP_ID}:fingerprint-1")
    existing_revision = type("Revision", (), {"id": existing_id, "revision_number": 7})()
    system_map = SystemMap(organisation_id=_ORG_ID, name="Delivery", account_id=_USER_ID, id=_MAP_ID)
    repository = AsyncMock()
    repository.get_map_by_id.return_value = system_map
    repository.list_revisions.return_value = [existing_revision]
    repository.create_revision.return_value = type("Revision", (), {"id": uuid.uuid4(), "revision_number": 8})()
    session = AsyncMock()
    session.execute.return_value = _Result(None)

    _, returned_id, revision_number = await SystemMapDiscoveryRepository(repository).save_discovery_revision(
        session,
        organisation_id=_ORG_ID,
        map_id=_MAP_ID,
        content={"discovery_fingerprint": "fingerprint-2", "nodes": [], "relationships": []},
        updated_by=_USER_ID,
    )

    expected_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{_MAP_ID}:fingerprint-2")
    assert returned_id == expected_id
    assert revision_number == 8
    repository.create_revision.assert_awaited_once()
    assert repository.create_revision.await_args.kwargs["revision_id"] == expected_id


async def test_save_discovery_revision_requires_existing_map() -> None:
    repository = AsyncMock()
    repository.get_map_by_id.return_value = None
    session = AsyncMock()

    with pytest.raises(ValueError, match="system map discovery draft not found"):
        await SystemMapDiscoveryRepository(repository).save_discovery_revision(
            session,
            organisation_id=_ORG_ID,
            map_id=_MAP_ID,
            content={"discovery_fingerprint": "fingerprint-1"},
            updated_by=_USER_ID,
        )

    repository.create_revision.assert_not_awaited()
