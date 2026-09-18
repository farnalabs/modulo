"""Opaque 60s single-use WS tokens backed by Redis."""

import json
import logging
import secrets
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

_log = logging.getLogger(__name__)

_KEY_PREFIX = "ws_token:"


class WsTokenConsumeError(Exception):
    """Raised when a Redis error prevents WS token consumption."""


class WsTokenExpiredError(Exception):
    """Raised when a WS token has expired or was already consumed."""


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
        _log.exception("ws_token.create_failed", extra={"error": str(exc)})
        raise
    return token


async def consume_ws_token(
    redis: Redis,
    token: str,
) -> dict[str, Any]:
    """Atomic single-use consumption of a WS token.

    Returns the stored principal dict if valid.
    Raises WsTokenExpired if the token is expired or already used.
    Raises WsTokenConsumeError if Redis is unreachable or returns an error.
    """
    key = _KEY_PREFIX + token
    try:
        data = await redis.getdel(key)
    except RedisTimeoutError:
        _log.exception("ws_token.consume_timeout")
        raise WsTokenConsumeError("Redis timeout while consuming WS token") from None
    except RedisError as exc:
        _log.exception("ws_token.consume_failed", extra={"error": str(exc)})
        raise WsTokenConsumeError(f"Redis error: {exc}") from exc
    if data is None:
        raise WsTokenExpiredError("WS token expired or already used")
    try:
        decoded = json.loads(data.decode()) if isinstance(data, bytes) else json.loads(data)
        if not isinstance(decoded, dict):
            raise ValueError("WS token payload must be an object")
        return cast("dict[str, Any]", decoded)
    except (ValueError, TypeError) as exc:
        _log.exception("ws_token.corrupt_data", extra={"error": str(exc)})
        raise WsTokenConsumeError(f"Corrupt WS token data: {exc}") from exc
