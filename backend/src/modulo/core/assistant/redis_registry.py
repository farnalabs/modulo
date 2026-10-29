"""Redis connection lifecycle for the Assistant."""

from __future__ import annotations

import redis.asyncio as aioredis


class AssistantRedisRegistry:
    """Own the Assistant Redis client and close it during shutdown."""

    def __init__(self, redis_url: str) -> None:
        self._redis: aioredis.Redis = aioredis.Redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()
