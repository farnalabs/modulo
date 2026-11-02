"""Redis connection lifecycle for the Assistant."""

from __future__ import annotations

import redis.asyncio as aioredis


class AssistantRedisRegistry:
    """Own the Assistant Redis client and close it during shutdown."""

    def __init__(self, redis_url: str) -> None:
        self._redis: aioredis.Redis = aioredis.Redis.from_url(redis_url, decode_responses=True)

    async def clear_session(self, session_id: str) -> None:
        """Delete any legacy per-session Assistant keys left in Redis."""
        keys = [f"assistant:ui_results:{session_id}", f"assistant:approval:{session_id}"]
        await self._redis.delete(*keys)

    async def close(self) -> None:
        await self._redis.aclose()
