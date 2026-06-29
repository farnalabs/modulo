"""Unit tests for RedisEventBroker — all Redis calls are mocked."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.events.redis_broker import CHANNEL_PREFIX, RedisEventBroker


@pytest.fixture
def mock_redis() -> MagicMock:
    """Return a mock Redis client that responds to from_url."""
    client = MagicMock(spec_set=["publish", "close", "pubsub", "from_url"])
    client.publish = AsyncMock()
    client.close = AsyncMock()
    client.pubsub = MagicMock()
    return client


@pytest.fixture
async def broker(mock_redis: MagicMock) -> RedisEventBroker:
    """Return a RedisEventBroker with both connections pre-mocked."""
    b = RedisEventBroker("redis://mock:6379/0")
    b._pub = mock_redis
    b._sub = mock_redis
    return b


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


async def test_connect_creates_two_connections():
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_client = MagicMock(spec=["publish", "close"])
        mock_client.publish = AsyncMock()
        mock_client.close = AsyncMock()
        mock_from_url.return_value = mock_client

        broker = RedisEventBroker("redis://test:6379/0")
        await broker.connect()

        assert mock_from_url.call_count == 2
        for call_args in mock_from_url.call_args_list:
            assert call_args[0][0] == "redis://test:6379/0"
            assert call_args[1] == {"decode_responses": True}

        assert broker._pub is mock_client
        assert broker._sub is mock_client


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


async def test_publish_sends_json_to_correct_channel(broker, mock_redis):
    await broker.publish("run:abc", {"event": "node_started", "node_id": "a"})

    expected_data = json.dumps({"event": "node_started", "node_id": "a"})
    mock_redis.publish.assert_awaited_once_with(f"{CHANNEL_PREFIX}run:abc", expected_data)


async def test_publish_auto_connects_when_already_connected():
    broker = RedisEventBroker("redis://test:6379/0")
    broker._pub = AsyncMock()
    broker._pub.publish = AsyncMock()
    broker._sub = MagicMock()

    with patch.object(RedisEventBroker, "connect", new_callable=AsyncMock) as mock_connect:
        await broker.publish("test", {"msg": "hello"})
        mock_connect.assert_not_awaited()


async def test_publish_auto_connects_when_not_connected():
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        mock_from_url.return_value = mock_client

        broker = RedisEventBroker("redis://test:6379/0")
        broker._sub = MagicMock()
        broker._pub = None

        await broker.publish("test", {"msg": "hello"})

        assert mock_from_url.call_count == 2
        mock_client.publish.assert_awaited_once()


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


async def test_subscribe_subscribes_to_correct_channel(broker, mock_redis):
    mock_pubsub = MagicMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_redis.pubsub.return_value = mock_pubsub

    result = await broker.subscribe("run:xyz")

    mock_redis.pubsub.assert_called_once()
    mock_pubsub.subscribe.assert_awaited_once_with(f"{CHANNEL_PREFIX}run:xyz")
    assert result is mock_pubsub


async def test_subscribe_auto_connects_when_not_connected():
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_client = MagicMock()
        mock_client.pubsub.return_value = MagicMock(subscribe=AsyncMock())
        mock_from_url.return_value = mock_client

        broker = RedisEventBroker("redis://test:6379/0")
        broker._pub = MagicMock()
        # _sub is None by default — subscribe() will call connect()
        await broker.subscribe("x")

        assert mock_from_url.call_count == 2
        assert broker._sub is mock_client


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_closes_both_connections(broker, mock_redis):
    await broker.close()

    assert mock_redis.close.await_count == 2
    assert broker._pub is None
    assert broker._sub is None


async def test_close_is_idempotent():
    broker = RedisEventBroker("redis://test:6379/0")
    broker._pub = None
    broker._sub = None
    await broker.close()  # must not raise
