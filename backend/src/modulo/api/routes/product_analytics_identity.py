"""Routes for product analytics instance identity & secret rotation."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import raise_session_contract_error
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_system_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.product_analytics.hmac_verify import verify_hmac
from modulo.core.product_analytics.instance_identity import (
    _INSTANCE_ID_KEY,
    get_or_create_instance_identity,
    get_secret_exists,
    rotate_secret,
)
from modulo.db.crud.row_lock import set_mutation_row_lock_timeout
from modulo.db.crud.system_config import get_config, update_config
from modulo.db.models.system_config import SystemConfig
from modulo.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, sqlstate_of

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

# A plain dict (not a defaultdict): every read is an explicit ``.get()`` and
# every write an explicit assignment, so a stray read can never silently grow
# the map past its bound.
_rotation_timestamps: dict[str, list[float]] = {}
_MAX_ROTATIONS = 5
_ROTATION_WINDOW = 3600.0  # 1 hour
# Hard bound on the number of tracked client keys. When the map reaches this
# size, idle keys are swept first; if it is still at the bound the
# least-recently-used keys are evicted, so a burst of distinct client IPs can
# never grow the map without limit. (A key is only pruned when a request
# arrives, so without this bound one-off client IPs accumulate forever.)
_MAX_TRACKED_CLIENTS = 10_000


def _sweep_expired_clients(window_start: float) -> None:
    """Drop client keys that hold no timestamps inside the current window.

    A client with no rotations left in the active window can never trip the
    limiter again, so its key carries no rate-limiting information and is safe
    to evict.
    """
    stale = [key for key, stamps in _rotation_timestamps.items() if not any(t > window_start for t in stamps)]
    for key in stale:
        del _rotation_timestamps[key]


def _touch_client(client_key: str, timestamps: list[float]) -> None:
    """Store ``timestamps`` for ``client_key``, marking it most-recently-used.

    Re-inserting an existing key moves it to the end of the dict's insertion
    order, so ``_evict_least_recently_used`` evicts the client that has gone
    longest without a rotation request rather than the one that has been
    tracked longest.
    """
    _rotation_timestamps.pop(client_key, None)
    _rotation_timestamps[client_key] = timestamps


def _evict_least_recently_used(limit: int) -> None:
    """Evict least-recently-used client keys until at most ``limit`` remain.

    Backstop for the case the sweep cannot help with: every tracked client
    still holds an in-window timestamp, yet the map is at its bound. Evicting
    the least-recently-used keys keeps memory hard-bounded while sparing the
    clients that are actively rotating (mirrors the drop-oldest bound used by
    the demo rate-limit floor).
    """
    while len(_rotation_timestamps) > limit:
        del _rotation_timestamps[next(iter(_rotation_timestamps))]


def _check_rotation_rate_limit(client_key: str) -> None:
    """Raise 429 if the client has exceeded the rotation rate limit."""
    now = time.time()
    window_start = now - _ROTATION_WINDOW
    # Prune old entries for the current key (read-only access avoids creating
    # an entry for a client that is not being tracked).
    timestamps = [t for t in _rotation_timestamps.get(client_key, ()) if t > window_start]
    if len(timestamps) >= _MAX_ROTATIONS:
        _touch_client(client_key, timestamps)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rotation rate limit exceeded. Max {_MAX_ROTATIONS} per hour.",
        )
    # Keep the tracked-client map hard-bounded before admitting a new key:
    # sweep idle clients first (cheap), then evict the least-recently-used keys
    # if the sweep could not bring the map under the cap. Evict down to one
    # below the cap so the incoming key fits without exceeding the bound.
    if client_key not in _rotation_timestamps and len(_rotation_timestamps) >= _MAX_TRACKED_CLIENTS:
        _sweep_expired_clients(window_start)
        if len(_rotation_timestamps) >= _MAX_TRACKED_CLIENTS:
            _log.warning(
                "%s: rotation rate-limiter at capacity (%d tracked clients); evicting least-recently-used",
                _LOG_ROTATE,
                _MAX_TRACKED_CLIENTS,
            )
            _evict_least_recently_used(_MAX_TRACKED_CLIENTS - 1)
    timestamps.append(now)
    _touch_client(client_key, timestamps)


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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "product_analytics_identity.get_identity")
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
    dependencies=[
        Depends(
            audited(
                "identity_secret_rotated",
                "product_analytics_identity",
                principal_dep=get_current_tenant_user,
                fail_closed=True,
            ),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        ),
        Depends(deny_break_glass_mint),
    ],
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
            # Bound every row-lock wait taken for the rest of this transaction
            # BEFORE the first lock (canonical FAR-1313 pattern), so a contended
            # rotation cannot wedge on an unbounded ``FOR UPDATE``.
            await set_mutation_row_lock_timeout(session)
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

            # Check sequence monotonicity — store last sequence in SystemConfig.
            # ``for_update=True`` takes a per-instance row lock so the
            # read-check-write below is atomic against concurrent rotations.
            last_seq = await _get_last_sequence(session, str(instance_id), for_update=True)
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
    except SequenceStateError:
        _log.error("%s.sequence_state_inconsistent", _LOG_ROTATE)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rotation state is inconsistent: a required identity record is missing. Refusing to rotate.",
        ) from None
    except ProgrammingError:
        _log.exception(_LOG_ROTATE)
        raise HTTPException(
            status_code=501,
            detail="Database not available. Run migrations.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "product_analytics_identity.rotate_identity_secret")
        if sqlstate_of(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            # A bounded lock wait expired (55P03): the DB is healthy, another
            # rotation simply holds the row lock. 409, never a retry-inviting 503.
            _log.warning("%s.lock_timeout", _LOG_ROTATE)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Timed out waiting for the rotation lock; another rotation is in progress. "
                    "Re-issue the request once it completes."
                ),
            ) from None
        _log.exception(_LOG_ROTATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception(_LOG_ROTATE)
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None


# ── helpers ─────────────────────────────────────────────────────────────────


_SEQUENCE_KEY_PREFIX = "product_analytics_last_sequence_"
_HAS_ROTATED_KEY_PREFIX = "product_analytics_has_rotated_"


class SequenceStateError(RuntimeError):
    """Stored rotation-sequence state is inconsistent and cannot be trusted.

    Raised when the per-instance sequence row is missing although a rotation has
    already happened (the marker is set).  Continuing would reset the monotonic
    guard to ``0`` and let previously-accepted sequences be re-minted, so the
    rotation fails closed instead.
    """


def _sequence_key(instance_id: str) -> str:
    return _SEQUENCE_KEY_PREFIX + instance_id


def _has_rotated_key(instance_id: str) -> str:
    return _HAS_ROTATED_KEY_PREFIX + instance_id


async def _lock_rotation_anchor(session: AsyncSession) -> None:
    """Take a transaction-scoped row lock that serialises rotations per instance.

    The lock targets the per-instance identity row rather than the sequence row
    because the latter does not exist before the first rotation — locking a
    not-yet-existing row would not stop two concurrent first rotations from both
    reading a missing sequence and both rotating.  The identity row is always
    present by this point (``get_or_create_instance_identity`` runs first in the
    same transaction), so ``SELECT … FOR UPDATE`` on it serialises the whole
    read-check-write critical section until the transaction commits.

    The row is verified after the select: a ``FOR UPDATE`` that matches no row
    takes no lock, so a missing anchor row must fail closed rather than silently
    degrading to an unserialised rotation.
    """
    anchor = (
        await session.execute(select(SystemConfig).where(SystemConfig.key == _INSTANCE_ID_KEY).with_for_update())
    ).scalar_one_or_none()
    if anchor is None:
        raise SequenceStateError(
            "Rotation identity anchor row is missing; refusing to rotate without a serialising lock."
        )


async def _get_last_sequence(
    session: AsyncSession,
    instance_id: str,
    *,
    for_update: bool = False,
) -> int:
    """Read the last accepted sequence number for this instance.

    When ``for_update`` is set, first takes a row lock (see
    :func:`_lock_rotation_anchor`) so the caller's read-check-write of the
    sequence is atomic: a concurrent rotation with the same sequence blocks on
    the lock, then observes the committed value and is rejected.

    A missing sequence row is legitimate only *before the first rotation* and
    reads as ``0``.  Once a rotation has happened (the has-rotated marker is
    set) a missing row means the guard was tampered with, and this fails closed
    with :class:`SequenceStateError` rather than silently resetting to ``0``.

    A stored value that is not a JSON integer also fails CLOSED, with a 500:
    accepting a corrupt value — or returning a default for one — would silently
    reset the monotonicity guard and let a caller replay an old sequence. A
    non-integer JSON number (float/bool) and every non-number shape
    (string/``null``/list/object) are all treated as corrupt.
    """
    if for_update:
        await _lock_rotation_anchor(session)
    key = _sequence_key(instance_id)
    entry = await get_config(session, key)
    if entry is None:
        if await _has_rotated(session, instance_id):
            raise SequenceStateError(
                "Rotation sequence row is missing although this instance has already rotated; "
                "refusing to reset the monotonic guard and re-mint old sequences."
            )
        return 0
    value = entry.value
    if isinstance(value, bool) or not isinstance(value, int):
        _log.error(
            "%s: corrupt stored sequence for key %r: expected an integer, got %r",
            _LOG_ROTATE,
            key,
            value,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Corrupt product-analytics rotation sequence config at key '{key}': "
                "stored value is not a valid integer. Refusing to rotate; repair the "
                "SystemConfig value before retrying."
            ),
        )
    return int(value)


async def _has_rotated(session: AsyncSession, instance_id: str) -> bool:
    """Return True if a rotation has ever been recorded for this instance."""
    marker = await get_config(session, _has_rotated_key(instance_id))
    return bool(marker.value) if marker is not None else False


async def _set_last_sequence(session: AsyncSession, instance_id: str, seq: int) -> None:
    """Persist the last accepted sequence number and the has-rotated marker."""
    await update_config(session, _sequence_key(instance_id), seq)
    await update_config(session, _has_rotated_key(instance_id), True)


def _constant_time_equal(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
