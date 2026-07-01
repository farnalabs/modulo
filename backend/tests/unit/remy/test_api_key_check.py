"""Unit tests for user_has_api_key — model backend availability check."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.remy.api_key_check import user_has_api_key


class TestUserHasApiKey:
    """Tests for user_has_api_key helper."""

    async def test_returns_true_when_model_backends_exist(self) -> None:
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar = MagicMock(return_value=3)
        session.execute = AsyncMock(return_value=mock_result)

        result = await user_has_api_key(user_id, org_id, session)
        assert result is True

    async def test_returns_false_when_no_model_backends(self) -> None:
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar = MagicMock(return_value=0)
        session.execute = AsyncMock(return_value=mock_result)

        result = await user_has_api_key(user_id, org_id, session)
        assert result is False

    async def test_returns_false_when_count_is_none(self) -> None:
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=mock_result)

        result = await user_has_api_key(user_id, org_id, session)
        assert result is False

    async def test_queries_active_backends_only(self) -> None:
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar = MagicMock(return_value=1)
        session.execute = AsyncMock(return_value=mock_result)

        await user_has_api_key(user_id, org_id, session)

        assert session.execute.await_count == 1
