"""Coverage for RepositoryHub dispatch and tenant-aware forwarding."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from farna.db.repositories import RepositoryHub
from farna.db.repositories.generic import GenericRepository
from farna.db.repositories.postgres import PostgresRepository

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.anyio
async def test_repository_hub_forwards_context_filter_pagination_and_execute() -> None:
    repo = MagicMock()
    repo.set_org_context = AsyncMock()
    repo.paginate = AsyncMock(return_value="page")
    repo.execute = AsyncMock(return_value="result")
    hub = RepositoryHub(session_factory=MagicMock(), db_type="sqlite")
    hub._repo = repo
    session = MagicMock()
    statement = select(1)

    await hub.set_org_context(session, _ORG_ID)
    filtered = hub.apply_tenant_filter(statement, _ORG_ID)
    page = await hub.paginate(session, statement, 2, 3)
    result = await hub.execute(session, statement)

    repo.set_org_context.assert_awaited_once_with(session, _ORG_ID)
    repo.apply_tenant_filter.assert_called_once_with(statement, _ORG_ID)
    repo.paginate.assert_awaited_once_with(session, statement, 2, 3)
    repo.execute.assert_awaited_once_with(session, statement)
    assert filtered is repo.apply_tenant_filter.return_value
    assert page == "page"
    assert result == "result"


def test_repository_hub_uses_settings_when_db_type_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = MagicMock(modulo_db="POSTGRES")
    monkeypatch.setattr("farna.settings.get_settings", lambda: settings)
    hub = RepositoryHub(session_factory=MagicMock())
    assert isinstance(hub.repo, PostgresRepository)


def test_unknown_backend_falls_back_to_generic_repository() -> None:
    hub = RepositoryHub(session_factory=MagicMock(), db_type="unknown")
    assert isinstance(hub.repo, GenericRepository)
