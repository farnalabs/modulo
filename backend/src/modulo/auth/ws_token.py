"""Opaque 60s single-use WS tokens backed by Redis."""

import json
import logging
import secrets
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

_log = logging.getLogger(__name__)

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
    try:
        payload = json.dumps(principal_json, default=str)
        await redis.setex(key, ttl, payload)
    except (TypeError, RedisError) as exc:
        _log.error("ws_token.create_failed", extra={"error": str(exc)})
        raise
    return token


async def consume_ws_token(
    redis: Redis,
    token: str,
) -> dict[str, Any] | None:
    """Atomic single-use consumption of a WS token.

    Returns the stored principal dict if valid, None if expired or already used.
    """
    key = _KEY_PREFIX + token
    try:
        data = await redis.getdel(key)
    except RedisError as exc:
        _log.error("ws_token.consume_failed", extra={"error": str(exc)})
        return None
    if data is None:
        return None
    if isinstance(data, bytes):
        return json.loads(data.decode())
    return json.loads(data)
