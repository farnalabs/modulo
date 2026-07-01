"""Unit tests for opaque 60s single-use WS tokens."""

import json
from unittest.mock import AsyncMock

import pytest

from modulo.auth.ws_token import consume_ws_token, create_ws_token


@pytest.fixture
def mock_redis() -> AsyncMock:
    return AsyncMock()


_PRINCIPAL = {
    "sub": "alice@example.com",
    "org_id": "00000000-0000-0000-0000-000000000001",
    "user_id": "11111111-1111-1111-1111-111111111111",
    "org_role": "admin",
}


class TestCreateWsToken:
    async def test_returns_url_safe_opaque_string(self, mock_redis: AsyncMock) -> None:
        token = await create_ws_token(mock_redis, _PRINCIPAL)

        assert isinstance(token, str)
        assert len(token) > 0
        # secrets.token_urlsafe(32) produces 43 chars, all URL-safe
        assert token, "token should not be empty"
        assert all(c.isalnum() or c in "-_" for c in token)

    async def test_calls_redis_setex(self, mock_redis: AsyncMock) -> None:
        token = await create_ws_token(mock_redis, _PRINCIPAL, ttl=60)

        mock_redis.setex.assert_awaited_once()
        args, _kwargs = mock_redis.setex.await_args
        key = args[0]
        assert key.startswith("ws_token:")
        assert key.removeprefix("ws_token:") == token
        stored = args[1]
        assert stored == 60
        assert json.loads(args[2]) == _PRINCIPAL

    async def test_default_ttl_is_60(self, mock_redis: AsyncMock) -> None:
        await create_ws_token(mock_redis, _PRINCIPAL)

        mock_redis.setex.assert_awaited_once()
        args, _kwargs = mock_redis.setex.await_args
        assert args[1] == 60

    async def test_custom_ttl(self, mock_redis: AsyncMock) -> None:
        await create_ws_token(mock_redis, _PRINCIPAL, ttl=120)

        mock_redis.setex.assert_awaited_once()
        args, _kwargs = mock_redis.setex.await_args
        assert args[1] == 120

    async def test_unique_tokens(self, mock_redis: AsyncMock) -> None:
        t1 = await create_ws_token(mock_redis, _PRINCIPAL)
        t2 = await create_ws_token(mock_redis, _PRINCIPAL)
        assert t1 != t2


class TestConsumeWsToken:
    async def test_returns_principal_for_valid_token(self, mock_redis: AsyncMock) -> None:
        token = await create_ws_token(mock_redis, _PRINCIPAL)

        mock_redis.reset_mock()
        expected_json = json.dumps(_PRINCIPAL)
        mock_redis.getdel.return_value = expected_json

        result = await consume_ws_token(mock_redis, token)

        assert result == _PRINCIPAL
        mock_redis.getdel.assert_awaited_once_with(f"ws_token:{token}")

    async def test_handles_bytes_from_redis(self, mock_redis: AsyncMock) -> None:
        token = await create_ws_token(mock_redis, _PRINCIPAL)

        mock_redis.reset_mock()
        mock_redis.getdel.return_value = json.dumps(_PRINCIPAL).encode()

        result = await consume_ws_token(mock_redis, token)
        assert result == _PRINCIPAL

    async def test_returns_none_for_expired_token(self, mock_redis: AsyncMock) -> None:
        mock_redis.getdel.return_value = None

        result = await consume_ws_token(mock_redis, "nonexistent-token")
        assert result is None

    async def test_returns_none_for_wrong_key(self, mock_redis: AsyncMock) -> None:
        mock_redis.getdel.return_value = None

        result = await consume_ws_token(mock_redis, "wrong-key")
        assert result is None

    async def test_single_use(self, mock_redis: AsyncMock) -> None:
        token = await create_ws_token(mock_redis, _PRINCIPAL)

        mock_redis.reset_mock()
        mock_redis.getdel.side_effect = [json.dumps(_PRINCIPAL), None]

        first = await consume_ws_token(mock_redis, token)
        second = await consume_ws_token(mock_redis, token)

        assert first == _PRINCIPAL
        assert second is None
        assert mock_redis.getdel.await_count == 2

    async def test_consumed_token_cannot_be_reused(self, mock_redis: AsyncMock) -> None:
        """Integration-style: create then consume twice via mocked Redis."""
        token = await create_ws_token(mock_redis, _PRINCIPAL)

        mock_redis.reset_mock()
        mock_redis.getdel.side_effect = [json.dumps(_PRINCIPAL), None]

        first = await consume_ws_token(mock_redis, token)
        second = await consume_ws_token(mock_redis, token)

        assert first == _PRINCIPAL
        assert second is None
