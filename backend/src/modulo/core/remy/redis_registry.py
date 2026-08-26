"""Redis-backed registry for Remy distributed state, replacing in-memory dicts."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable
from typing import Any, cast

import redis.asyncio as aioredis

from modulo.util import sanitise_log_value as _sanitise_log_value

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]


async def _redis_result[T](result: Awaitable[T] | T) -> T:
    """Resolve redis-py's sync-or-async stub union for the async client."""
    if isinstance(result, Awaitable):
        return await result
    return result


def _json_object(value: object) -> JsonObject | None:
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        return None
    return cast("JsonObject", decoded)


def _json_object_list(value: object) -> list[JsonObject] | None:
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        return None
    return cast("list[JsonObject]", decoded)


class RemyRedisRegistry:
    """Multi-worker safe Redis backend for Remy's in-flight state.

    Redis is required — raises on construction if connection fails.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis: aioredis.Redis = aioredis.Redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()

    # ── Permission request state ───────────────────────────────────────────

    async def set_permission_request(
        self, request_id: str, session_id: str, tools: list[JsonObject], ttl: int = 120
    ) -> None:
        key = f"remy:permission:{request_id}"
        await _redis_result(self._redis.hset(key, mapping={"session_id": session_id, "tools": json.dumps(tools)}))
        await _redis_result(self._redis.expire(key, ttl))

    async def get_permission_request(self, request_id: str) -> JsonObject | None:
        data = cast("dict[str, str]", await _redis_result(self._redis.hgetall(f"remy:permission:{request_id}")))
        if not data:
            return None
        try:
            tools = _json_object_list(data.get("tools", "[]"))
            if tools is None:
                raise ValueError("permission tools must be a list of objects")
        except (ValueError, TypeError):
            logger.warning("Invalid JSON in permission request tools for %s", _sanitise_log_value(request_id))
            tools = []
        return {"session_id": data.get("session_id", ""), "tools": tools}

    async def set_permission_decision(self, request_id: str, decision: JsonObject, ttl: int = 120) -> None:
        await _redis_result(self._redis.setex(f"remy:decision:{request_id}", ttl, json.dumps(decision)))

    async def get_and_clear_permission_decision(self, request_id: str) -> JsonObject | None:
        val = await _redis_result(self._redis.get(f"remy:decision:{request_id}"))
        if val is None:
            return None
        await _redis_result(self._redis.delete(f"remy:decision:{request_id}"))
        try:
            return _json_object(val)
        except (ValueError, TypeError):
            logger.warning("Invalid JSON in permission decision for %s", request_id)
            return None

    # ── UI command results state ───────────────────────────────────────────

    async def set_ui_command_results(self, session_id: str, results: list[JsonObject], ttl: int = 300) -> None:
        await _redis_result(self._redis.setex(f"remy:ui_results:{session_id}", ttl, json.dumps(results)))

    async def get_and_clear_ui_command_results(self, session_id: str) -> list[JsonObject]:
        key = f"remy:ui_results:{session_id}"
        val = await _redis_result(self._redis.get(key))
        if val is None:
            return []
        await _redis_result(self._redis.delete(key))
        try:
            return _json_object_list(val) or []
        except (ValueError, TypeError):
            logger.warning("Invalid JSON in UI command results for %s", session_id)
            return []

    # ── Session approvals ──────────────────────────────────────────────────

    async def set_session_approval(self, session_id: str, tool_name: str, page_path: str, ttl: int = 1800) -> None:
        key = f"remy:approval:{session_id}"
        await _redis_result(
            self._redis.hset(
                key,
                tool_name,
                json.dumps({"page_path": page_path, "expires_at": time.time() + ttl}),
            )
        )
        await _redis_result(self._redis.expire(key, ttl + 60))

    async def is_session_approved(self, session_id: str, tool_name: str, page_path: str) -> bool:
        key = f"remy:approval:{session_id}"
        val = await _redis_result(self._redis.hget(key, tool_name))
        if val is None:
            return False
        try:
            data = _json_object(val)
            if data is None:
                return False
            expires_at = data.get("expires_at")
            return (
                data.get("page_path") == page_path and isinstance(expires_at, (int, float)) and expires_at > time.time()
            )
        except (ValueError, TypeError):
            return False

    async def clear_session_approvals(self, session_id: str) -> None:
        await _redis_result(self._redis.delete(f"remy:approval:{session_id}"))

    async def clear_session(self, session_id: str) -> None:
        """Remove all Redis keys for a given session."""
        keys = [f"remy:ui_results:{session_id}", f"remy:approval:{session_id}"]
        await _redis_result(self._redis.delete(*keys))

    # ── Publish / subscribe for cross-worker event signalling ──────────────

    async def publish_permission_response(self, request_id: str, decision: JsonObject) -> None:
        await _redis_result(self._redis.publish(f"remy:channel:permission:{request_id}", json.dumps(decision)))

    async def subscribe_permission_response(self, request_id: str, timeout: float = 60.0) -> JsonObject | None:  # noqa: ASYNC109
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"remy:channel:permission:{request_id}")
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            if message and message.get("data"):
                try:
                    return _json_object(message["data"])
                except (ValueError, TypeError):
                    logger.warning("Invalid JSON in permission pubsub response for %s", request_id)
                    return None
            return None
        finally:
            await pubsub.unsubscribe(f"remy:channel:permission:{request_id}")
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def publish_ui_results(self, session_id: str) -> None:
        await _redis_result(self._redis.publish(f"remy:channel:ui_results:{session_id}", "ready"))

    async def subscribe_ui_results(self, session_id: str, timeout: float = 120.0) -> bool:  # noqa: ASYNC109
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"remy:channel:ui_results:{session_id}")
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            return message is not None
        finally:
            await pubsub.unsubscribe(f"remy:channel:ui_results:{session_id}")
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def publish_resume(self, session_id: str) -> None:
        await _redis_result(self._redis.publish(f"remy:channel:resume:{session_id}", "resume"))

    async def subscribe_resume(self, session_id: str, timeout: float = 300.0) -> bool:  # noqa: ASYNC109
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"remy:channel:resume:{session_id}")
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            return message is not None
        finally:
            await pubsub.unsubscribe(f"remy:channel:resume:{session_id}")
            await pubsub.aclose()  # type: ignore[no-untyped-call]
