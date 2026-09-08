"""Unit tests for ``modulo.db.crud.agent_runner_binding`` (FAR-592 / D6).

Uses an AsyncMock session (no real DB) to cover the save-time validation
surfaces, the wholesale-replace semantics, and the teardown/delete inventory
helpers. Resolution of actual credentials is covered by the integration suite.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from modulo.db.crud.agent_runner_binding import (
    count_bindings_for_backend,
    delete_all_bindings_for_agent,
    delete_binding,
    delete_org_binding_rows,
    get_binding,
    list_bindings_for_agent,
    replace_agent_bindings,
)
from modulo.db.models.agent_runner_binding import AgentRunnerBinding
from modulo.db.models.model_backend import ModelBackend

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_AGENT_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()
_ACCOUNT_ID = uuid.uuid4()
_BINDING_ID = uuid.uuid4()


def _backend(provider: str = "openai") -> MagicMock:
    b = MagicMock(spec=ModelBackend)
    b.id = _BACKEND_ID
    b.organisation_id = _ORG_ID
    b.provider = provider
    b.visibility = "org"
    return b


def _session(backend: MagicMock) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = [backend]
    result.scalar_one_or_none.return_value = None
    result.rowcount = 5
    session.execute.return_value = result
    return session


def _binding_row() -> MagicMock:
    b = MagicMock(spec=AgentRunnerBinding)
    b.id = _BINDING_ID
    b.organisation_id = _ORG_ID
    b.agent_id = _AGENT_ID
    b.model_backend_id = _BACKEND_ID
    b.target_env_var = "OPENAI_API_KEY"
    b.source_field = "api_key"
    b.account_id = _ACCOUNT_ID
    return b


async def test_get_binding_returns_row() -> None:
    session = AsyncMock()
    row = _binding_row()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute.return_value = result
    assert await get_binding(session, _BINDING_ID) is row


async def test_list_bindings_for_agent_orders_asc() -> None:
    session = AsyncMock()
    rows = [_binding_row(), _binding_row()]
    result = MagicMock()
    result.scalars.return_value = rows
    session.execute.return_value = result
    out = await list_bindings_for_agent(session, _AGENT_ID)
    assert out == rows


async def test_replace_agent_bindings_happy() -> None:
    backend = _backend()
    session = _session(backend)
    specs = [
        {
            "_backend_id": _BACKEND_ID,
            "target_env_var": "OPENAI_API_KEY",
            "source_field": "api_key",
            "_account_id": _ACCOUNT_ID,
        }
    ]
    created = await replace_agent_bindings(session, org_id=_ORG_ID, agent_id=_AGENT_ID, bindings_specs=specs)
    assert len(created) == 1
    assert isinstance(created[0], AgentRunnerBinding)
    assert created[0].target_env_var == "OPENAI_API_KEY"
    session.add.assert_called()


async def test_replace_agent_bindings_unknown_backend_400() -> None:
    session = _session(_backend())
    specs = [
        {
            "_backend_id": uuid.uuid4(),  # not in backends_by_id
            "target_env_var": "OPENAI_API_KEY",
            "source_field": "api_key",
            "_account_id": _ACCOUNT_ID,
        }
    ]
    with pytest.raises(HTTPException) as exc:
        await replace_agent_bindings(session, org_id=_ORG_ID, agent_id=_AGENT_ID, bindings_specs=specs)
    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST


async def test_replace_agent_bindings_duplicate_target_409() -> None:
    session = _session(_backend())
    specs = [
        {
            "_backend_id": _BACKEND_ID,
            "target_env_var": "OPENAI_API_KEY",
            "source_field": "api_key",
            "_account_id": _ACCOUNT_ID,
        },
        {
            "_backend_id": _BACKEND_ID,
            "target_env_var": "OPENAI_API_KEY",
            "source_field": "api_key",
            "_account_id": _ACCOUNT_ID,
        },
    ]
    with pytest.raises(HTTPException) as exc:
        await replace_agent_bindings(session, org_id=_ORG_ID, agent_id=_AGENT_ID, bindings_specs=specs)
    assert exc.value.status_code == status.HTTP_409_CONFLICT


async def test_replace_agent_bindings_integrity_error_409() -> None:
    backend = _backend()
    session = _session(backend)
    session.flush.side_effect = IntegrityError("dup", {}, Exception())
    specs = [
        {
            "_backend_id": _BACKEND_ID,
            "target_env_var": "OPENAI_API_KEY",
            "source_field": "api_key",
            "_account_id": _ACCOUNT_ID,
        },
    ]
    with pytest.raises(HTTPException) as exc:
        await replace_agent_bindings(session, org_id=_ORG_ID, agent_id=_AGENT_ID, bindings_specs=specs)
    assert exc.value.status_code == status.HTTP_409_CONFLICT


async def test_delete_all_bindings_for_agent_returns_rowcount() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 3
    session.execute.return_value = result
    assert await delete_all_bindings_for_agent(session, agent_id=_AGENT_ID) == 3


async def test_delete_binding_present() -> None:
    session = AsyncMock()
    row = _binding_row()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute.return_value = result
    assert await delete_binding(session, binding_id=_BINDING_ID, agent_id=_AGENT_ID) is True
    session.delete.assert_called_once_with(row)


async def test_delete_binding_absent() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    assert await delete_binding(session, binding_id=_BINDING_ID, agent_id=_AGENT_ID) is False


async def test_count_bindings_for_backend() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = 7
    session.execute.return_value = result
    assert await count_bindings_for_backend(session, model_backend_id=_BACKEND_ID, org_id=_ORG_ID) == 7


async def test_delete_org_binding_rows_returns_rowcount() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 4
    session.execute.return_value = result
    assert await delete_org_binding_rows(session, org_id=_ORG_ID) == 4
