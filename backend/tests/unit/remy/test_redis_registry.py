"""Unit tests for RemyRedisRegistry — all Redis calls are mocked."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.remy.redis_registry import (
    RemyRedisRegistry,
    _json_object,
    _json_object_list,
    _redis_result,
)

# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [{"a": 1}, "abc", b"bytes", 42, None, [1, 2], 3.14],
)
async def test_redis_result_passthrough_plain_value(value: object) -> None:
    assert await _redis_result(value) is value


async def test_redis_result_awaits_awaitable() -> None:
    async def _coro() -> int:
        return 7

    assert await _redis_result(_coro()) == 7


class TestJsonObject:
    def test_str_dict(self) -> None:
        assert _json_object('{"a": 1}') == {"a": 1}

    def test_bytes_dict(self) -> None:
        assert _json_object(b'{"a": 1}') == {"a": 1}

    def test_bytearray_dict(self) -> None:
        assert _json_object(bytearray(b'{"a": 1}')) == {"a": 1}

    def test_non_string_value_returns_none(self) -> None:
        assert _json_object({"a": 1}) is None
        assert _json_object(42) is None
        assert _json_object(None) is None

    def test_json_list_returns_none(self) -> None:
        assert _json_object("[1, 2]") is None

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _json_object("{not json")


class TestJsonObjectList:
    def test_valid_list(self) -> None:
        assert _json_object_list('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]

    def test_list_with_non_dict_returns_none(self) -> None:
        assert _json_object_list('[{"a": 1}, 2]') is None

    def test_non_list_returns_none(self) -> None:
        assert _json_object_list('{"a": 1}') is None
        assert _json_object_list(42) is None

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _json_object_list("not-json")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_client() -> MagicMock:
    """A mock redis.asyncio.Redis client with async methods ready to configure."""
    client = MagicMock()
    client.aclose = AsyncMock()
    client.hset = AsyncMock()
    client.hgetall = AsyncMock(return_value={})
    client.expire = AsyncMock()
    client.setex = AsyncMock()
    client.get = AsyncMock(return_value=None)
    client.delete = AsyncMock(return_value=1)
    client.publish = AsyncMock()
    client.pubsub = MagicMock()
    return client


@pytest.fixture
def registry(redis_client: MagicMock) -> RemyRedisRegistry:
    """Return a RemyRedisRegistry whose underlying Redis client is mocked."""
    with patch("redis.asyncio.Redis.from_url", return_value=redis_client) as mock_from_url:
        reg = RemyRedisRegistry("redis://localhost:6379/0")
    mock_from_url.assert_called_once_with("redis://localhost:6379/0", decode_responses=True)
    return reg


def _pubsub(channel: str, message: dict[str, Any] | None) -> MagicMock:
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()

    async def _get_message(**_: Any) -> dict[str, Any] | None:
        if message is None:
            return None
        return {"channel": channel, "data": json.dumps(message)}

    pubsub.get_message = AsyncMock(side_effect=_get_message)
    return pubsub


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_constructor_creates_redis_client_with_decode_responses(redis_client: MagicMock) -> None:
    with patch("redis.asyncio.Redis.from_url") as mock_from_url:
        mock_from_url.return_value = redis_client
        reg = RemyRedisRegistry("redis://localhost:6379/0")
    mock_from_url.assert_called_once_with("redis://localhost:6379/0", decode_responses=True)
    assert reg._redis is redis_client


async def test_close_closes_client(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    await registry.close()
    redis_client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Permission request state
# ---------------------------------------------------------------------------


async def test_set_permission_request_hsets_and_expires(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    tools = [{"name": "run_pipeline", "args": {"pipeline_id": "p1"}}]
    await registry.set_permission_request("req-1", "sess-1", tools, ttl=120)

    redis_client.hset.assert_awaited_once_with(
        "remy:permission:req-1",
        mapping={"session_id": "sess-1", "tools": json.dumps(tools)},
    )
    redis_client.expire.assert_awaited_once_with("remy:permission:req-1", 120)


async def test_get_permission_request_happy_path(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    tools = [{"name": "run_pipeline"}]
    redis_client.hgetall.return_value = {"session_id": "sess-1", "tools": json.dumps(tools)}
    result = await registry.get_permission_request("req-1")
    assert result == {"session_id": "sess-1", "tools": tools}


async def test_get_permission_request_empty_returns_none(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.hgetall.return_value = {}
    assert await registry.get_permission_request("req-1") is None


async def test_get_permission_request_missing_tools_defaults_empty(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    redis_client.hgetall.return_value = {"session_id": "sess-1"}
    result = await registry.get_permission_request("req-1")
    assert result == {"session_id": "sess-1", "tools": []}


async def test_get_permission_request_invalid_tools_warns_and_defaults_empty(
    registry: RemyRedisRegistry, redis_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    redis_client.hgetall.return_value = {"session_id": "sess-1", "tools": "{not json"}
    with caplog.at_level("WARNING", logger="modulo.core.remy.redis_registry"):
        result = await registry.get_permission_request("req-1")
    assert result == {"session_id": "sess-1", "tools": []}
    assert "Invalid JSON in permission request tools" in caplog.text


async def test_get_permission_request_tools_not_list_warns(
    registry: RemyRedisRegistry, redis_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    redis_client.hgetall.return_value = {"session_id": "sess-1", "tools": '{"name": "x"}'}
    with caplog.at_level("WARNING", logger="modulo.core.remy.redis_registry"):
        result = await registry.get_permission_request("req-1")
    assert result == {"session_id": "sess-1", "tools": []}
    assert "Invalid JSON in permission request tools" in caplog.text


# ---------------------------------------------------------------------------
# Permission decisions
# ---------------------------------------------------------------------------


async def test_set_permission_decision_setex(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    decision = {"decision": "allow"}
    await registry.set_permission_decision("req-1", decision, ttl=120)
    redis_client.setex.assert_awaited_once_with("remy:decision:req-1", 120, json.dumps(decision))


async def test_get_and_clear_permission_decision_happy_path(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    decision = {"decision": "deny"}
    redis_client.get.return_value = json.dumps(decision)
    result = await registry.get_and_clear_permission_decision("req-1")
    assert result == decision
    redis_client.delete.assert_awaited_once_with("remy:decision:req-1")


async def test_get_and_clear_permission_decision_missing_returns_none(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    redis_client.get.return_value = None
    assert await registry.get_and_clear_permission_decision("req-1") is None
    redis_client.delete.assert_not_called()


async def test_get_and_clear_permission_decision_invalid_json(
    registry: RemyRedisRegistry, redis_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    redis_client.get.return_value = "{not json"
    with caplog.at_level("WARNING", logger="modulo.core.remy.redis_registry"):
        result = await registry.get_and_clear_permission_decision("req-1")
    assert result is None
    assert "Invalid JSON in permission decision" in caplog.text
    redis_client.delete.assert_awaited_once_with("remy:decision:req-1")


async def test_get_and_clear_permission_decision_non_object_json(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    redis_client.get.return_value = "[1, 2]"
    result = await registry.get_and_clear_permission_decision("req-1")
    assert result is None
    redis_client.delete.assert_awaited_once_with("remy:decision:req-1")


# ---------------------------------------------------------------------------
# UI command results
# ---------------------------------------------------------------------------


async def test_set_ui_command_results_setex(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    results = [{"result": "ok"}]
    await registry.set_ui_command_results("sess-1", results, ttl=300)
    redis_client.setex.assert_awaited_once_with("remy:ui_results:sess-1", 300, json.dumps(results))


async def test_get_and_clear_ui_command_results_happy_path(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    results = [{"result": "ok"}]
    redis_client.get.return_value = json.dumps(results)
    result = await registry.get_and_clear_ui_command_results("sess-1")
    assert result == results
    redis_client.delete.assert_awaited_once_with("remy:ui_results:sess-1")


async def test_get_and_clear_ui_command_results_missing_returns_empty(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    redis_client.get.return_value = None
    assert await registry.get_and_clear_ui_command_results("sess-1") == []
    redis_client.delete.assert_not_called()


async def test_get_and_clear_ui_command_results_invalid_json_warns(
    registry: RemyRedisRegistry, redis_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    redis_client.get.return_value = "{not json"
    with caplog.at_level("WARNING", logger="modulo.core.remy.redis_registry"):
        result = await registry.get_and_clear_ui_command_results("sess-1")
    assert result == []
    assert "Invalid JSON in UI command results" in caplog.text
    redis_client.delete.assert_awaited_once_with("remy:ui_results:sess-1")


# ---------------------------------------------------------------------------
# Session approvals
# ---------------------------------------------------------------------------


async def test_set_session_approval_hsets_and_expires(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    await registry.set_session_approval("sess-1", "run_pipeline", "/pipelines", ttl=1800)

    redis_client.hset.assert_awaited_once()
    args = redis_client.hset.call_args
    assert args.args[0] == "remy:approval:sess-1"
    assert args.args[1] == "run_pipeline"
    stored = json.loads(args.args[2])
    assert stored["page_path"] == "/pipelines"
    assert stored["expires_at"] > time.time()
    redis_client.expire.assert_awaited_once_with("remy:approval:sess-1", 1800 + 60)


async def test_is_session_approved_happy_path(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    payload = json.dumps({"page_path": "/pipelines", "expires_at": time.time() + 1000})
    redis_client.hget.return_value = payload
    assert await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines") is True


async def test_is_session_approved_wrong_page_path(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    payload = json.dumps({"page_path": "/other", "expires_at": time.time() + 1000})
    redis_client.hget.return_value = payload
    assert await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines") is False


async def test_is_session_approved_expired(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    payload = json.dumps({"page_path": "/pipelines", "expires_at": time.time() - 1000})
    redis_client.hget.return_value = payload
    assert await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines") is False


async def test_is_session_approved_missing_value(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.hget.return_value = None
    assert await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines") is False


async def test_is_session_approved_invalid_json(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.hget.return_value = "{not json"
    assert await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines") is False


async def test_is_session_approved_missing_expires_at(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.hget.return_value = json.dumps({"page_path": "/pipelines"})
    assert await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines") is False


async def test_is_session_approved_wrong_expires_at_type(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.hget.return_value = json.dumps({"page_path": "/pipelines", "expires_at": "soon"})
    assert await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines") is False


async def test_clear_session_approvals_deletes_key(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    await registry.clear_session_approvals("sess-1")
    redis_client.delete.assert_awaited_once_with("remy:approval:sess-1")


async def test_clear_session_deletes_all_keys(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    await registry.clear_session("sess-1")
    redis_client.delete.assert_awaited_once_with("remy:ui_results:sess-1", "remy:approval:sess-1")


# ---------------------------------------------------------------------------
# Publish / subscribe
# ---------------------------------------------------------------------------


async def test_publish_permission_response(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    decision = {"decision": "allow"}
    await registry.publish_permission_response("req-1", decision)
    redis_client.publish.assert_awaited_once_with("remy:channel:permission:req-1", json.dumps(decision))


async def test_subscribe_permission_response_receives_message(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    decision = {"decision": "allow"}
    redis_client.pubsub.return_value = _pubsub("remy:channel:permission:req-1", decision)
    result = await registry.subscribe_permission_response("req-1", timeout=1.0)
    assert result == decision
    redis_client.pubsub.return_value.subscribe.assert_awaited_once_with("remy:channel:permission:req-1")
    redis_client.pubsub.return_value.unsubscribe.assert_awaited_once()
    redis_client.pubsub.return_value.aclose.assert_awaited_once()


async def test_subscribe_permission_response_timeout_returns_none(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    redis_client.pubsub.return_value = _pubsub("remy:channel:permission:req-1", None)
    result = await registry.subscribe_permission_response("req-1", timeout=0.01)
    assert result is None


async def test_subscribe_permission_response_invalid_json_warns(
    registry: RemyRedisRegistry, redis_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()

    async def _bad_message(**_: Any) -> dict[str, str]:
        return {"channel": "c", "data": "{not json"}

    pubsub.get_message = AsyncMock(side_effect=_bad_message)
    redis_client.pubsub.return_value = pubsub
    with caplog.at_level("WARNING", logger="modulo.core.remy.redis_registry"):
        result = await registry.subscribe_permission_response("req-1", timeout=1.0)
    assert result is None
    assert "Invalid JSON in permission pubsub response" in caplog.text


async def test_publish_ui_results(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    await registry.publish_ui_results("sess-1")
    redis_client.publish.assert_awaited_once_with("remy:channel:ui_results:sess-1", "ready")


async def test_subscribe_ui_results_receives_message(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.pubsub.return_value = _pubsub("remy:channel:ui_results:sess-1", {"x": 1})
    assert await registry.subscribe_ui_results("sess-1", timeout=1.0) is True
    redis_client.pubsub.return_value.unsubscribe.assert_awaited_once()
    redis_client.pubsub.return_value.aclose.assert_awaited_once()


async def test_subscribe_ui_results_timeout_returns_false(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.pubsub.return_value = _pubsub("remy:channel:ui_results:sess-1", None)
    assert await registry.subscribe_ui_results("sess-1", timeout=0.01) is False


async def test_publish_resume(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    await registry.publish_resume("sess-1")
    redis_client.publish.assert_awaited_once_with("remy:channel:resume:sess-1", "resume")


async def test_subscribe_resume_receives_message(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.pubsub.return_value = _pubsub("remy:channel:resume:sess-1", {"x": 1})
    assert await registry.subscribe_resume("sess-1", timeout=1.0) is True
    redis_client.pubsub.return_value.unsubscribe.assert_awaited_once()
    redis_client.pubsub.return_value.aclose.assert_awaited_once()


async def test_subscribe_resume_timeout_returns_false(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.pubsub.return_value = _pubsub("remy:channel:resume:sess-1", None)
    assert await registry.subscribe_resume("sess-1", timeout=0.01) is False


# ---------------------------------------------------------------------------
# Cancellation safety
# ---------------------------------------------------------------------------


async def test_get_permission_request_cancelled_propagates(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    redis_client.hgetall.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await registry.get_permission_request("req-1")


async def test_is_session_approved_cancelled_propagates(registry: RemyRedisRegistry, redis_client: MagicMock) -> None:
    redis_client.hget.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await registry.is_session_approved("sess-1", "run_pipeline", "/pipelines")


async def test_subscribe_permission_response_cancelled_propagates(
    registry: RemyRedisRegistry, redis_client: MagicMock
) -> None:
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock(side_effect=asyncio.CancelledError())
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    redis_client.pubsub.return_value = pubsub
    with pytest.raises(asyncio.CancelledError):
        await registry.subscribe_permission_response("req-1", timeout=1.0)
    pubsub.aclose.assert_not_called()
