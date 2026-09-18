"""Routes for product analytics instance identity & secret rotation."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_system_permission
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.product_analytics.hmac_verify import verify_hmac
from modulo.core.product_analytics.instance_identity import (
    get_or_create_instance_identity,
    get_secret_exists,
    rotate_secret,
)
from modulo.db.crud.system_config import get_config, update_config

_log = logging.getLogger(__name__)

_LOG_IDENTITY = "product_analytics.get_identity"
_LOG_ROTATE = "product_analytics.rotate_secret"

router = APIRouter(
    prefix="/api/v1/product-analytics",
    tags=["product-analytics-identity"],
)

# ── Rate limiter for rotation (in-memory, per-process) ─────────────────────
# Note: this rate limiter is per-process and not shared across workers behind
# a load balancer; a Redis-backed limiter would be required for that.

_rotation_timestamps: dict[str, list[float]] = defaultdict(list)
_MAX_ROTATIONS = 5
_ROTATION_WINDOW = 3600.0  # 1 hour


def _check_rotation_rate_limit(client_key: str) -> None:
    """Raise 429 if the client has exceeded the rotation rate limit."""
    now = time.time()
    window_start = now - _ROTATION_WINDOW
    timestamps = _rotation_timestamps[client_key]
    # Prune old entries
    _rotation_timestamps[client_key] = [t for t in timestamps if t > window_start]
    if len(_rotation_timestamps[client_key]) >= _MAX_ROTATIONS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rotation rate limit exceeded. Max {_MAX_ROTATIONS} per hour.",
        )
    _rotation_timestamps[client_key].append(now)


# ── Response models ─────────────────────────────────────────────────────────


class IdentityResponse(BaseModel):
    instance_id: str = Field(..., description="UUID of this Modulo instance")
    secret_exists: bool = Field(..., description="Whether a shared secret has been minted")


class RotateRequest(BaseModel):
    old_secret: str = Field(..., description="Current secret used to authenticate the rotation")
    timestamp: float = Field(..., description="Unix timestamp when the request was signed")
    sequence: int = Field(..., description="Monotonic per-instance sequence number")
    hmac_digest: str = Field(..., description="HMAC-SHA256 hex digest over (payload, timestamp, sequence)")


class RotateResponse(BaseModel):
    new_secret: str = Field(..., description="The newly generated secret")


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get(
    "/identity",
    responses={
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
async def get_identity(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _current_user: AuthenticatedPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> IdentityResponse:
    """Return instance_id and whether a secret exists (never the secret itself).

    System-admin only.
    """
    try:
        async with session.begin():
            instance_id, _secret = await get_or_create_instance_identity(session)
            secret_exists = await get_secret_exists(session)
        return IdentityResponse(
            instance_id=str(instance_id),
            secret_exists=secret_exists,
        )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except ProgrammingError:
        _log.exception(_LOG_IDENTITY)
        raise HTTPException(
            status_code=501,
            detail="Database not available. Run migrations.",
        ) from None
    except SQLAlchemyError:
        _log.exception(_LOG_IDENTITY)
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception(_LOG_IDENTITY)
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None


@router.post(
    "/rotate",
    dependencies=[Depends(deny_break_glass_mint)],
    responses={
        400: {"description": "Bad Request"},
        401: {"description": "Unauthorized"},
        429: {"description": "Too Many Requests"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
async def rotate_identity_secret(
    req: RotateRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _current_user: AuthenticatedPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> RotateResponse:
    """Rotate the shared secret, authenticated by the old secret.

    Rate-limited to 5 rotations per hour per client IP.
    Distinguishes 401 (auth failure / clock skew) from 403 (permission) from 400.
    """
    client_key = request.client.host if request.client else "unknown"
    _check_rotation_rate_limit(client_key)

    try:
        async with session.begin():
            instance_id, current_secret = await get_or_create_instance_identity(session)

            # Verify the old secret matches
            if not _constant_time_equal(req.old_secret, current_secret):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication failed. Verify the old secret is correct.",
                )

            # Verify HMAC (replay protection)
            # Use old_secret for HMAC verification — the client signed with it.
            payload_bytes = str(instance_id).encode("utf-8")
            if not verify_hmac(
                req.old_secret,
                payload_bytes,
                req.timestamp,
                req.sequence,
                req.hmac_digest,
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="HMAC verification failed. Check timestamp clock skew (5-min window).",
                )

            # Check sequence monotonicity — store last sequence in SystemConfig
            last_seq = await _get_last_sequence(session, str(instance_id))
            if req.sequence <= last_seq:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Sequence must be > {last_seq}. Out-of-order requests are rejected.",
                )
            await _set_last_sequence(session, str(instance_id), req.sequence)

            new_secret = await rotate_secret(session)

        return RotateResponse(new_secret=new_secret)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except ProgrammingError:
        _log.exception(_LOG_ROTATE)
        raise HTTPException(
            status_code=501,
            detail="Database not available. Run migrations.",
        ) from None
    except SQLAlchemyError:
        _log.exception(_LOG_ROTATE)
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception(_LOG_ROTATE)
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None


# ── helpers ─────────────────────────────────────────────────────────────────


_SEQUENCE_KEY_PREFIX = "product_analytics_last_sequence_"


async def _get_last_sequence(session: AsyncSession, instance_id: str) -> int:
    """Read the last accepted sequence number for this instance."""
    key = _SEQUENCE_KEY_PREFIX + instance_id
    entry = await get_config(session, key)
    if entry is None:
        return 0
    return int(entry.value)


async def _set_last_sequence(session: AsyncSession, instance_id: str, seq: int) -> None:
    """Persist the last accepted sequence number."""
    key = _SEQUENCE_KEY_PREFIX + instance_id
    await update_config(session, key, seq)


def _constant_time_equal(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
