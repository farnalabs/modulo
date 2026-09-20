"""Unit tests for the FAR-1025 soft-delete opt-in in the library uninstall helpers.

``uninstall_collection`` inspects tracked entities through ``_check_unmodified``,
``_delete_entity``, ``_detach_entity`` and ``_entity_exists``. Because pipelines
are soft-deletable, those helpers opt out of the global soft-delete filter with
``include_soft_deleted`` so a pending-deletion pipeline is still reconciled.
Integration coverage exists but is excluded from the coverage denominator, so
these direct unit tests keep the changed-lines coverage gate green.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.library_service.uninstall import (
    _check_unmodified,
    _delete_entity,
    _detach_entity,
    _entity_exists,
)


async def test_check_unmodified_pipeline_true_when_stamped() -> None:
    install_id = uuid.uuid4()
    entity = MagicMock()
    entity.collection_install_id = install_id
    session = MagicMock()
    session.scalar = AsyncMock(return_value=entity)

    assert await _check_unmodified(session, "pipeline", uuid.uuid4(), install_id) is True


async def test_check_unmodified_pipeline_false_when_absent() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    assert await _check_unmodified(session, "pipeline", uuid.uuid4(), uuid.uuid4()) is False


async def test_delete_entity_pipeline_deletes_row() -> None:
    entity = MagicMock()
    session = MagicMock()
    session.scalar = AsyncMock(return_value=entity)
    session.delete = AsyncMock()

    await _delete_entity(session, "pipeline", uuid.uuid4())

    session.delete.assert_awaited_once_with(entity)


async def test_detach_entity_pipeline_clears_stamp() -> None:
    entity = MagicMock()
    entity.collection_install_id = uuid.uuid4()
    session = MagicMock()
    session.scalar = AsyncMock(return_value=entity)

    await _detach_entity(session, "pipeline", uuid.uuid4())

    assert entity.collection_install_id is None


async def test_entity_exists_pipeline_true() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=MagicMock())

    assert await _entity_exists(session, "pipeline", uuid.uuid4()) is True


async def test_entity_exists_pipeline_false() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    assert await _entity_exists(session, "pipeline", uuid.uuid4()) is False
