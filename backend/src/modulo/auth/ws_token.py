"""Opaque 60s single-use WS tokens backed by Redis."""

import json
import secrets
from typing import Any

from redis.asyncio import Redis

_KEY_PREFIX = "ws_token:"


async def create_ws_token(
    redis: Redis,
    principal_json: dict[str, Any],
    ttl: int = 60,
) -> str:
    """Generate an opaque single-use WS token and store it in Redis.

    Returns the raw token string (the caller gets it once).
    Uses GETDEL for atomic single-use consumption.
    """
    token = secrets.token_urlsafe(32)
    key = _KEY_PREFIX + token
    await redis.setex(key, ttl, json.dumps(principal_json))
    return token


async def consume_ws_token(
    redis: Redis,
    token: str,
) -> dict[str, Any] | None:
    """Atomic single-use consumption of a WS token.

    Returns the stored principal dict if valid, None if expired or already used.
    """
    key = _KEY_PREFIX + token
    data = await redis.getdel(key)
    if data is None:
        return None
    if isinstance(data, bytes):
        return json.loads(data.decode())
    return json.loads(data)
