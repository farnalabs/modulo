from __future__ import annotations

import asyncio

"""Redis-backed registry for Remy distributed state, replacing in-memory dicts."""


import json
import logging
import time

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class RemyRedisRegistry:
    """Multi-worker safe Redis backend for Remy's in-flight state.

    Gracefully falls back to no-op (state disabled) when Redis is unavailable,
    so single-worker deployments continue working without Redis.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis: aioredis.Redis | None = None
        self._redis_url = redis_url

    async def _get_redis(self) -> aioredis.Redis | None:
        if self._redis is None:
            try:
                self._redis = aioredis.Redis.from_url(self._redis_url, decode_responses=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Redis unavailable for Remy state - falling back to in-memory only")
                return None
        return self._redis

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # ── Permission request state ───────────────────────────────────────────

    async def set_permission_request(self, request_id: str, session_id: str, tools: list[dict], ttl: int = 120) -> None:
        r = await self._get_redis()
        if r is None:
            return
        key = f"remy:permission:{request_id}"
        await r.hset(key, mapping={"session_id": session_id, "tools": json.dumps(tools)})
        await r.expire(key, ttl)

    async def get_permission_request(self, request_id: str) -> dict | None:
        r = await self._get_redis()
        if r is None:
            return None
        data = await r.hgetall(f"remy:permission:{request_id}")
        if not data:
            return None
        return {"session_id": data.get("session_id", ""), "tools": json.loads(data.get("tools", "[]"))}

    async def set_permission_decision(self, request_id: str, decision: dict, ttl: int = 120) -> None:
        r = await self._get_redis()
        if r is None:
            return
        await r.setex(f"remy:decision:{request_id}", ttl, json.dumps(decision))

    async def get_and_clear_permission_decision(self, request_id: str) -> dict | None:
        r = await self._get_redis()
        if r is None:
            return None
        val = await r.get(f"remy:decision:{request_id}")
        if val is None:
            return None
        await r.delete(f"remy:decision:{request_id}")
        return json.loads(val)

    # ── UI command results state ───────────────────────────────────────────

    async def set_ui_command_results(self, session_id: str, results: list[dict], ttl: int = 300) -> None:
        r = await self._get_redis()
        if r is None:
            return
        await r.setex(f"remy:ui_results:{session_id}", ttl, json.dumps(results))

    async def get_and_clear_ui_command_results(self, session_id: str) -> list[dict]:
        r = await self._get_redis()
        if r is None:
            return []
        key = f"remy:ui_results:{session_id}"
        val = await r.get(key)
        if val is None:
            return []
        await r.delete(key)
        return json.loads(val)

    # ── Session approvals ──────────────────────────────────────────────────

    async def set_session_approval(self, session_id: str, tool_name: str, page_path: str, ttl: int = 1800) -> None:
        r = await self._get_redis()
        if r is None:
            return
        key = f"remy:approval:{session_id}"
        await r.hset(
            key,
            tool_name,
            json.dumps({"page_path": page_path, "expires_at": time.time() + ttl}),
        )
        await r.expire(key, ttl + 60)

    async def is_session_approved(self, session_id: str, tool_name: str, page_path: str) -> bool:
        r = await self._get_redis()
        if r is None:
            return False
        key = f"remy:approval:{session_id}"
        val = await r.hget(key, tool_name)
        if val is None:
            return False
        try:
            data = json.loads(val)
            return data.get("page_path") == page_path and data.get("expires_at", 0) > time.time()
        except (ValueError, TypeError, json.JSONDecodeError):
            return False

    async def clear_session_approvals(self, session_id: str) -> None:
        r = await self._get_redis()
        if r is None:
            return
        await r.delete(f"remy:approval:{session_id}")

    async def clear_session(self, session_id: str) -> None:
        """Remove all Redis keys for a given session."""
        r = await self._get_redis()
        if r is None:
            return
        keys = [f"remy:ui_results:{session_id}", f"remy:approval:{session_id}"]
        await r.delete(*keys)

    # ── Publish / subscribe for cross-worker event signalling ──────────────

    async def publish_permission_response(self, request_id: str, decision: dict) -> None:
        r = await self._get_redis()
        if r is None:
            return
        await r.publish(f"remy:channel:permission:{request_id}", json.dumps(decision))

    async def subscribe_permission_response(self, request_id: str, timeout: float = 60.0) -> dict | None:  # noqa: ASYNC109
        r = await self._get_redis()
        if r is None:
            return None
        pubsub = r.pubsub()
        await pubsub.subscribe(f"remy:channel:permission:{request_id}")
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            if message and message.get("data"):
                return json.loads(message["data"])
            return None
        finally:
            await pubsub.unsubscribe(f"remy:channel:permission:{request_id}")

    async def publish_ui_results(self, session_id: str) -> None:
        r = await self._get_redis()
        if r is None:
            return
        await r.publish(f"remy:channel:ui_results:{session_id}", "ready")

    async def subscribe_ui_results(self, session_id: str, timeout: float = 120.0) -> bool:  # noqa: ASYNC109
        r = await self._get_redis()
        if r is None:
            return False
        pubsub = r.pubsub()
        await pubsub.subscribe(f"remy:channel:ui_results:{session_id}")
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            return message is not None
        finally:
            await pubsub.unsubscribe(f"remy:channel:ui_results:{session_id}")

    async def publish_resume(self, session_id: str) -> None:
        r = await self._get_redis()
        if r is None:
            return
        await r.publish(f"remy:channel:resume:{session_id}", "resume")

    async def subscribe_resume(self, session_id: str, timeout: float = 300.0) -> bool:  # noqa: ASYNC109
        r = await self._get_redis()
        if r is None:
            return False
        pubsub = r.pubsub()
        await pubsub.subscribe(f"remy:channel:resume:{session_id}")
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
            return message is not None
        finally:
            await pubsub.unsubscribe(f"remy:channel:resume:{session_id}")
