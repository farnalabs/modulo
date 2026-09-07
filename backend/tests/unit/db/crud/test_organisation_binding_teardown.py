"""Unit tests for Organisation teardown that deletes agent-runner binding rows (FAR-592 / D6).

``delete_organisation`` deletes the org's ``agent_runner_bindings`` rows explicitly
before the hard org delete (the binding -> model_backend FK is ON DELETE
RESTRICT). That path is only exercised by the integration suite, which does not
feed SonarCloud's new-code coverage — so a mocked unit test keeps the teardown
surface under unit coverage.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.db.crud import organisation as org_crud


@pytest.mark.asyncio
async def test_delete_organisation_missing_returns_false() -> None:
    org_id = uuid.uuid4()
    session = AsyncMock()
    with patch("modulo.db.crud.organisation.get_organisation", new=AsyncMock(return_value=None)):
        assert await org_crud.delete_organisation(session, org_id) is False


@pytest.mark.asyncio
async def test_delete_organisation_deletes_binding_rows_first() -> None:
    org_id = uuid.uuid4()
    session = AsyncMock()
    # ``session.delete`` / ``session.flush`` are no-ops on a mock.
    session.delete.return_value = None
    session.flush.return_value = None
    with (
        patch(
            "modulo.db.crud.organisation.get_organisation",
            new=AsyncMock(return_value=SimpleNamespace(id=org_id)),
        ),
        patch("modulo.db.rls.set_rls_org") as set_org,
        patch("modulo.db.rls.set_rls_execution_context") as set_ctx,
        patch("modulo.db.crud.agent_runner_binding.delete_org_binding_rows") as delete_rows,
    ):
        assert await org_crud.delete_organisation(session, org_id) is True
    set_org.assert_awaited_once()
    set_ctx.assert_awaited_once()
    delete_rows.assert_awaited_once_with(session, org_id)
    session.delete.assert_awaited_once()
    session.flush.assert_awaited_once()
