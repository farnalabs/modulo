"""Unit tests for RedisEventBroker — all Redis calls are mocked."""

from __future__ import annotations

import asyncio
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
def broker(mock_redis: MagicMock) -> RedisEventBroker:
    """Return a RedisEventBroker with both connections pre-mocked."""
    b = RedisEventBroker("redis://mock:6379/0")
    b._pub = mock_redis
    b._sub = mock_redis
    return b


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


def test_redact_url_masks_password() -> None:
    broker = RedisEventBroker("redis://localhost:6379/0")
    assert broker._redact_url("redis://:secret@localhost:6379/0") == "redis://:****@localhost:6379/0"


def test_redact_url_masks_password_with_username() -> None:
    broker = RedisEventBroker("redis://localhost:6379/0")
    assert broker._redact_url("redis://user:secret@localhost:6379/0") == "redis://user:****@localhost:6379/0"


def test_redact_url_without_password_is_unchanged() -> None:
    broker = RedisEventBroker("redis://localhost:6379/0")
    assert broker._redact_url("redis://localhost:6379/0") == "redis://localhost:6379/0"


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


async def test_connect_creates_two_connections() -> None:
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_client = MagicMock(spec=["publish", "close"])
        mock_client.publish = AsyncMock()
        mock_client.close = AsyncMock()
        mock_from_url.return_value = mock_client

        broker = RedisEventBroker("redis://test:6379/0")
        await broker.connect()

        assert mock_from_url.call_count == 2
        expected_kwargs = {"decode_responses": True, "socket_connect_timeout": 2.0, "socket_timeout": 5.0}
        for call_args in mock_from_url.call_args_list:
            assert call_args[0][0] == "redis://test:6379/0"
            assert call_args[1] == expected_kwargs

        assert broker._pub is mock_client
        assert broker._sub is mock_client


async def test_connect_is_idempotent_when_already_connected(broker: RedisEventBroker, mock_redis: MagicMock) -> None:
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        await broker.connect()

    mock_from_url.assert_not_called()
    assert broker._pub is mock_redis
    assert broker._sub is mock_redis


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


async def test_publish_sends_json_to_correct_channel(broker: RedisEventBroker, mock_redis: MagicMock) -> None:
    await broker.publish("run:abc", {"event": "node_started", "node_id": "a"})

    expected_data = json.dumps({"event": "node_started", "node_id": "a"})
    mock_redis.publish.assert_awaited_once_with(f"{CHANNEL_PREFIX}run:abc", expected_data)


async def test_publish_auto_connects_when_already_connected() -> None:
    broker = RedisEventBroker("redis://test:6379/0")
    broker._pub = AsyncMock()
    broker._sub = MagicMock()

    with patch.object(RedisEventBroker, "connect", new_callable=AsyncMock) as mock_connect:
        await broker.publish("test", {"msg": "hello"})
        mock_connect.assert_not_awaited()


async def test_publish_auto_connects_when_not_connected() -> None:
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_client = MagicMock()
        mock_client.publish = AsyncMock()
        mock_from_url.return_value = mock_client

        broker = RedisEventBroker("redis://test:6379/0")
        broker._sub = MagicMock()
        broker._pub = None

        await broker.publish("test", {"msg": "hello"})

        assert mock_from_url.call_count == 1
        mock_client.publish.assert_awaited_once()


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


async def test_subscribe_subscribes_to_correct_channel(broker: RedisEventBroker, mock_redis: MagicMock) -> None:
    mock_pubsub = MagicMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_redis.pubsub.return_value = mock_pubsub

    result = await broker.subscribe("run:xyz")

    mock_redis.pubsub.assert_called_once()
    mock_pubsub.subscribe.assert_awaited_once_with(f"{CHANNEL_PREFIX}run:xyz")
    assert result is mock_pubsub


async def test_subscribe_auto_connects_when_not_connected() -> None:
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_client = MagicMock()
        mock_client.pubsub.return_value = MagicMock(subscribe=AsyncMock())
        mock_from_url.return_value = mock_client

        broker = RedisEventBroker("redis://test:6379/0")
        broker._pub = MagicMock()
        # _sub is None by default — subscribe() will call connect()
        await broker.subscribe("x")

        assert mock_from_url.call_count == 1
        assert broker._sub is mock_client


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_closes_both_connections(broker: RedisEventBroker, mock_redis: MagicMock) -> None:
    await broker.close()

    assert mock_redis.close.await_count == 2
    assert broker._pub is None
    assert broker._sub is None


async def test_close_is_idempotent() -> None:
    broker = RedisEventBroker("redis://test:6379/0")
    broker._pub = None
    broker._sub = None
    await broker.close()  # must not raise
    assert broker._pub is None
    assert broker._sub is None


# ---------------------------------------------------------------------------
# Error-path connection cleanup
# ---------------------------------------------------------------------------


async def test_publish_error_closes_old_connection(broker: RedisEventBroker, mock_redis: MagicMock) -> None:
    """When pub.publish() raises, the old connection must be closed before clearing _pub."""
    mock_redis.publish.side_effect = ConnectionError("Redis connection lost")

    await broker.publish("run:abc", {"event": "test"})
    mock_redis.close.assert_awaited_once_with(close_connection_pool=True)
    assert broker._pub is None


async def test_publish_error_on_serialization_does_not_close_connection(
    broker: RedisEventBroker, mock_redis: MagicMock
) -> None:
    """A json.dumps TypeError should not close the connection or clear _pub."""
    await broker.publish("run:abc", {"event": object()})  # non-serializable

    mock_redis.close.assert_not_called()
    assert broker._pub is mock_redis


async def test_subscribe_error_closes_old_connection(broker: RedisEventBroker, mock_redis: MagicMock) -> None:
    """When sub.pubsub().subscribe() raises, the old connection must be closed before clearing _sub."""
    mock_pubsub = MagicMock()
    mock_pubsub.subscribe = AsyncMock(side_effect=ConnectionError("Redis connection lost"))
    mock_redis.pubsub.return_value = mock_pubsub

    with pytest.raises(ConnectionError):
        await broker.subscribe("run:xyz")

    mock_redis.close.assert_awaited_once_with(close_connection_pool=True)
    assert broker._sub is None


async def test_publish_error_on_no_connection_propagates(broker: RedisEventBroker, mock_redis: MagicMock) -> None:
    """When _pub is None and connect() fails, the error should propagate."""
    with patch.object(RedisEventBroker, "connect", side_effect=ConnectionError("Redis unavailable")):
        broker._pub = None
        broker._sub = MagicMock()
        with pytest.raises(ConnectionError):
            await broker.publish("test", {"msg": "hello"})


# ---------------------------------------------------------------------------
# No-connection fallback paths
# ---------------------------------------------------------------------------


async def test_publish_with_no_connection_after_connect_logs_and_returns(caplog: pytest.LogCaptureFixture) -> None:
    """If connect() returns without a usable _pub, publish logs and returns."""
    broker = RedisEventBroker("redis://test:6379/0")
    broker._pub = None
    broker._sub = MagicMock()
    with patch.object(RedisEventBroker, "connect", new=AsyncMock()):
        await broker.publish("run:abc", {"event": "test"})
    assert "redis_broker.publish_no_connection" in caplog.text


async def test_subscribe_with_no_connection_raises_runtime_error() -> None:
    """subscribe() with no established _sub must raise RuntimeError."""
    broker = RedisEventBroker("redis://test:6379/0")
    broker._sub = None
    broker._pub = MagicMock()
    with (
        patch.object(RedisEventBroker, "connect", new=AsyncMock()),
        pytest.raises(RuntimeError, match="not established"),
    ):
        await broker.subscribe("run:xyz")


# ---------------------------------------------------------------------------
# connect() failure and race paths
# ---------------------------------------------------------------------------


async def test_connect_closes_partial_connection_and_reraises() -> None:
    """When building the second client fails, the first must be closed."""
    first = MagicMock(spec=["publish", "close"])
    first.close = AsyncMock()

    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_from_url.side_effect = [first, ConnectionError("Redis unreachable")]
        broker = RedisEventBroker("redis://test:6379/0")
        with pytest.raises(ConnectionError):
            await broker.connect()

    first.close.assert_awaited_once()
    assert broker._pub is None
    assert broker._sub is None


async def test_connect_reraises_cancellation() -> None:
    """connect() must not swallow asyncio.CancelledError."""
    with patch("modulo.core.events.redis_broker.aioredis.from_url") as mock_from_url:
        mock_from_url.side_effect = asyncio.CancelledError()
        broker = RedisEventBroker("redis://test:6379/0")
        with pytest.raises(asyncio.CancelledError):
            await broker.connect()

    assert broker._pub is None
    assert broker._sub is None


async def test_connect_race_closes_duplicate_clients() -> None:
    """If another coroutine wins the race, freshly-built clients are closed."""
    broker = RedisEventBroker("redis://test:6379/0")
    winner = MagicMock(spec=["publish", "close"])
    winner.close = AsyncMock()
    dup1 = MagicMock(spec=["publish", "close"])
    dup1.close = AsyncMock()
    dup2 = MagicMock(spec=["publish", "close"])
    dup2.close = AsyncMock()
    dups = [dup1, dup2]

    def _side_effect(*_args: object, **_kwargs: object) -> MagicMock:
        broker._pub = winner
        broker._sub = winner
        return dups.pop(0)

    with patch("modulo.core.events.redis_broker.aioredis.from_url", side_effect=_side_effect):
        await broker.connect()

    dup1.close.assert_awaited_once()
    dup2.close.assert_awaited_once()
    assert broker._pub is winner
    assert broker._sub is winner


# ---------------------------------------------------------------------------
# Cancellation re-raise paths
# ---------------------------------------------------------------------------


async def test_publish_reraises_cancellation_without_clearing_connection() -> None:
    """A cancelled publish must leave the connection intact."""
    broker = RedisEventBroker("redis://test:6379/0")
    started = asyncio.Event()
    client = MagicMock(spec=["publish", "close"])

    async def _blocking_publish(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    client.publish = _blocking_publish
    client.close = AsyncMock()
    broker._pub = client
    broker._sub = MagicMock()

    task = asyncio.create_task(broker.publish("run:abc", {"event": "test"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    client.close.assert_not_called()
    assert broker._pub is client


async def test_subscribe_reraises_cancellation_without_clearing_connection() -> None:
    """A cancelled subscribe must leave the connection intact."""
    broker = RedisEventBroker("redis://test:6379/0")
    started = asyncio.Event()
    client = MagicMock(spec=["publish", "close", "pubsub"])
    mock_pubsub = MagicMock()

    async def _blocking_subscribe(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    mock_pubsub.subscribe = _blocking_subscribe
    client.pubsub.return_value = mock_pubsub
    client.close = AsyncMock()
    broker._pub = MagicMock()
    broker._sub = client

    task = asyncio.create_task(broker.subscribe("run:xyz"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    client.close.assert_not_called()
    assert broker._sub is client


# ---------------------------------------------------------------------------
# Close-failure warning paths
# ---------------------------------------------------------------------------


async def test_publish_close_failure_after_error_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """If closing the broken pub connection fails, a warning must be logged."""
    broker = RedisEventBroker("redis://test:6379/0")
    client = MagicMock(spec=["publish", "close"])
    client.publish = AsyncMock(side_effect=ConnectionError("Redis connection lost"))
    client.close = AsyncMock(side_effect=RuntimeError("close failed"))
    broker._pub = client
    broker._sub = MagicMock()

    await broker.publish("run:abc", {"event": "test"})

    assert broker._pub is None
    assert "redis_broker.pub_close_failed_after_error" in caplog.text


async def test_subscribe_close_failure_after_error_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """If closing the broken sub connection fails, a warning must be logged."""
    broker = RedisEventBroker("redis://test:6379/0")
    client = MagicMock(spec=["publish", "close", "pubsub"])
    client.pubsub.return_value = MagicMock(subscribe=AsyncMock(side_effect=ConnectionError("lost")))
    client.close = AsyncMock(side_effect=RuntimeError("close failed"))
    broker._pub = MagicMock()
    broker._sub = client

    with pytest.raises(ConnectionError):
        await broker.subscribe("run:xyz")

    assert broker._sub is None
    assert "redis_broker.sub_close_failed_after_error" in caplog.text


async def test_close_logs_warning_when_pub_close_fails(caplog: pytest.LogCaptureFixture) -> None:
    """close() must log and continue when the pub connection close fails."""
    broker = RedisEventBroker("redis://test:6379/0")
    pub = MagicMock(spec=["publish", "close"])
    pub.close = AsyncMock(side_effect=RuntimeError("close failed"))
    broker._pub = pub
    broker._sub = None

    await broker.close()

    assert broker._pub is None
    assert "redis_broker.pub_close_failed" in caplog.text


async def test_close_logs_warning_when_sub_close_fails(caplog: pytest.LogCaptureFixture) -> None:
    """close() must log and continue when the sub connection close fails."""
    broker = RedisEventBroker("redis://test:6379/0")
    sub = MagicMock(spec=["publish", "close"])
    sub.close = AsyncMock(side_effect=RuntimeError("close failed"))
    broker._pub = None
    broker._sub = sub

    await broker.close()

    assert broker._sub is None
    assert "redis_broker.sub_close_failed" in caplog.text


async def test_publish_reraises_cancellation_during_error_cleanup() -> None:
    """A cancellation while closing the broken pub connection must propagate."""
    broker = RedisEventBroker("redis://test:6379/0")
    client = MagicMock(spec=["publish", "close"])
    client.publish = AsyncMock(side_effect=ConnectionError("Redis connection lost"))
    cleanup_started = asyncio.Event()

    async def blocking_close(*_args: object, **_kwargs: object) -> None:
        cleanup_started.set()
        await asyncio.Event().wait()

    client.close = blocking_close
    broker._pub = client
    broker._sub = MagicMock()

    task = asyncio.create_task(broker.publish("run:abc", {"event": "test"}))
    await cleanup_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert broker._pub is None


async def test_subscribe_reraises_cancellation_during_error_cleanup() -> None:
    """A cancellation while closing the broken sub connection must propagate."""
    broker = RedisEventBroker("redis://test:6379/0")
    client = MagicMock(spec=["publish", "close", "pubsub"])
    client.pubsub.return_value = MagicMock(subscribe=AsyncMock(side_effect=ConnectionError("lost")))
    cleanup_started = asyncio.Event()

    async def blocking_close(*_args: object, **_kwargs: object) -> None:
        cleanup_started.set()
        await asyncio.Event().wait()

    client.close = blocking_close
    broker._pub = MagicMock()
    broker._sub = client

    task = asyncio.create_task(broker.subscribe("run:xyz"))
    await cleanup_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert broker._sub is None


async def test_close_reraises_cancellation_during_pub_close() -> None:
    """close() must not swallow a cancellation during pub connection close."""
    broker = RedisEventBroker("redis://test:6379/0")
    started = asyncio.Event()
    pub = MagicMock(spec=["publish", "close"])

    async def blocking_close(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    pub.close = blocking_close
    broker._pub = pub
    broker._sub = None

    task = asyncio.create_task(broker.close())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_close_reraises_cancellation_during_sub_close() -> None:
    """close() must not swallow a cancellation during sub connection close."""
    broker = RedisEventBroker("redis://test:6379/0")
    started = asyncio.Event()
    sub = MagicMock(spec=["publish", "close"])

    async def blocking_close(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    sub.close = blocking_close
    broker._pub = None
    broker._sub = sub

    task = asyncio.create_task(broker.close())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Module lazy re-exports
# ---------------------------------------------------------------------------


def test_module_getattr_exposes_redis_broker() -> None:
    import modulo.core.events as events
    from modulo.core.events.redis_broker import RedisEventBroker as BrokerClass

    assert events.RedisEventBroker is BrokerClass


def test_module_getattr_unknown_attribute_raises() -> None:
    import modulo.core.events as events

    with pytest.raises(AttributeError, match="has no attribute"):
        events.__getattr__("DoesNotExist")
