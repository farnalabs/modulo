"""Unit tests for the collection runnability check (FAR-762 / 0308 hardening).

``compute_runnable`` is derived on read: an install is runnable when every
connector requirement is ``configured+bound`` and the org has at least one
active model backend. A soft-deleted (uninstalled) install is RETIRED
history and is never runnable — the explicit ``deleted_at`` guard mirrors
``uninstall._load_install`` (0308), because ``session.get`` can resolve such
a row from the session identity map, bypassing the global soft-delete
filter.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.library_service.runnability import compute_runnable


def _install(
    *,
    org_id: uuid.UUID | None = None,
    checklist: list | None = None,
    deleted_at=None,
) -> MagicMock:
    """Create a mock CollectionInstall.

    ``deleted_at`` defaults to None explicitly: a bare ``MagicMock``
    auto-creates it as a truthy mock, which the retired-install guard
    rejects as already-uninstalled.
    """
    inst = MagicMock()
    inst.organisation_id = org_id or uuid.uuid4()
    inst.connector_checklist = checklist
    inst.deleted_at = deleted_at
    return inst


def _scalar_result(value) -> MagicMock:
    """A result whose ``scalar_one_or_none()`` returns *value*."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


class TestComputeRunnable:
    async def test_missing_install_is_not_runnable(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        assert await compute_runnable(session, uuid.uuid4()) is False

    async def test_soft_deleted_install_is_not_runnable(self) -> None:
        """A retired (uninstalled) install must never report runnable."""
        session = AsyncMock()
        session.get = AsyncMock(return_value=_install(deleted_at="2026-10-13T00:00:00+00:00"))
        assert await compute_runnable(session, uuid.uuid4()) is False

    async def test_live_install_with_model_backend_is_runnable(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=_install(checklist=[]))
        session.execute = AsyncMock(return_value=_scalar_result(uuid.uuid4()))
        assert await compute_runnable(session, uuid.uuid4()) is True

    async def test_unconfigured_connector_is_not_runnable(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=_install(checklist=[{"status": "missing"}]))
        assert await compute_runnable(session, uuid.uuid4()) is False

    async def test_no_model_backend_is_not_runnable(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=_install(checklist=[]))
        session.execute = AsyncMock(return_value=_scalar_result(None))
        assert await compute_runnable(session, uuid.uuid4()) is False
