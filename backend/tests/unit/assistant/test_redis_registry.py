"""Unit tests for AssistantRedisRegistry — all Redis calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.assistant.redis_registry import AssistantRedisRegistry


@pytest.fixture
def redis_client() -> MagicMock:
    """A mock redis.asyncio.Redis client with async methods ready to configure."""
    client = MagicMock()
    client.aclose = AsyncMock()
    client.delete = AsyncMock(return_value=2)
    return client


@pytest.fixture
def registry(redis_client: MagicMock) -> AssistantRedisRegistry:
    """Return an AssistantRedisRegistry whose underlying Redis client is mocked."""
    with patch("redis.asyncio.Redis.from_url", return_value=redis_client) as mock_from_url:
        reg = AssistantRedisRegistry("redis://localhost:6379/0")
    mock_from_url.assert_called_once_with("redis://localhost:6379/0", decode_responses=True)
    return reg


def test_constructor_creates_redis_client_with_decode_responses(redis_client: MagicMock) -> None:
    with patch("redis.asyncio.Redis.from_url") as mock_from_url:
        mock_from_url.return_value = redis_client
        reg = AssistantRedisRegistry("redis://localhost:6379/0")
    mock_from_url.assert_called_once_with("redis://localhost:6379/0", decode_responses=True)
    assert reg._redis is redis_client


async def test_close_closes_client(registry: AssistantRedisRegistry, redis_client: MagicMock) -> None:
    await registry.close()
    redis_client.aclose.assert_awaited_once()


async def test_clear_session_deletes_legacy_session_keys(
    registry: AssistantRedisRegistry, redis_client: MagicMock
) -> None:
    await registry.clear_session("sess-1")
    redis_client.delete.assert_awaited_once_with(
        "assistant:ui_results:sess-1",
        "assistant:approval:sess-1",
    )
