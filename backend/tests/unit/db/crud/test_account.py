"""CRUD shape tests for account helpers (FAR-1478 service provisioning)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.crud.account import create_service_account
from modulo.db.models.account import KIND_SERVICE


@pytest.mark.asyncio
async def test_create_service_account_shape() -> None:
    """A service row carries no human-shaped identity and kind='service'."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    account = await create_service_account(session, display_name="ci-bot")

    assert account.kind == KIND_SERVICE
    assert account.display_name == "ci-bot"
    assert account.email is None
    assert account.password_hash is None
    assert account.auth_provider is None
    assert account.sso_subject is None
    session.add.assert_called_once_with(account)
    session.flush.assert_awaited_once()
