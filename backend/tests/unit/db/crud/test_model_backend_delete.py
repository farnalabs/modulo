"""Unit tests for delete_model_backend in-use 409 mapping and not-found path."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from modulo.db.crud.model_backend import delete_model_backend


async def test_delete_model_backend_not_found_returns_false() -> None:
    """A missing backend short-circuits to False without touching the session."""
    model_backend_id = uuid.uuid4()
    session = AsyncMock()

    with patch(
        "modulo.db.crud.model_backend.get_model_backend",
        AsyncMock(return_value=None),
    ):
        result = await delete_model_backend(session, model_backend_id)

    assert result is False
    session.delete.assert_not_awaited()


async def test_delete_model_backend_blocked_in_use_raises_409() -> None:
    """A RESTRICT FK (in-use) maps to a typed 409 with remediation copy."""
    model_backend_id = uuid.uuid4()
    session = AsyncMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock(side_effect=IntegrityError("fk", None, Exception("blocked")))

    with (
        patch(
            "modulo.db.crud.model_backend.get_model_backend",
            AsyncMock(return_value=object()),
        ),
        pytest.raises(HTTPException) as excinfo,
    ):
        await delete_model_backend(session, model_backend_id)

    assert excinfo.value.status_code == 409
    assert "still in use" in excinfo.value.detail
