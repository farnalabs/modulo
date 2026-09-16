"""Admin API endpoints for Fernet key rotation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Annotated, Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    deny_break_glass_mint,
    get_db_session,
    require_system_permission,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.core.fernet_rotation import rotate_all_encrypted_data
from modulo.core.saq_worker import _make_system_session_factory
from modulo.settings import Settings, get_settings

_MIN_KEY_LEN = 32

# Redis key for the distributed rotation lock.  SET NX + TTL provides a
# cross-process atomic guard with self-healing on crash (the key expires
# automatically).  An in-process asyncio.Lock serialises concurrent callers
# within the same worker so two coroutines don't race on the Redis roundtrip.
_ROTATION_LOCK_KEY = "modulo:fernet_rotation:lock"
_ROTATION_LOCK_TTL_SECONDS = 1800  # 30 minutes — generous for large orgs

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/rotation", tags=["admin-rotation"])

# ── In-memory rotation state ──────────────────────────────────────────────

_last_rotation_result: dict[str, Any] | None = None
_rotation_lock = asyncio.Lock()


class RotateKeyRequest(BaseModel):
    new_fernet_key: str = Field(min_length=_MIN_KEY_LEN)
    old_fernet_key: str | None = Field(default=None, description="Previous key if different from current FERNET_KEY")


class RotateKeyResponse(BaseModel):
    status: str
    task_id: str
    message: str


class RotationStatusResponse(BaseModel):
    rotation_in_progress: bool
    last_rotation_result: dict[str, Any] | None = None


# ── Helpers ────────────────────────────────────────────────────────────────


def _validate_fernet_key(key: str, label: str) -> None:
    if len(key.encode()) < _MIN_KEY_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{label} must be at least {_MIN_KEY_LEN} bytes; got {len(key.encode())}",
        )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post(
    "/rotate-key",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(deny_break_glass_mint)],
    responses={
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("admin.rotation.rotate_key")
async def rotate_key(
    req: RotateKeyRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    current_user: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> RotateKeyResponse:
    """Start a Fernet key rotation.

    Re-encrypts all Fernet-encrypted data across all stores with the new key.
    The old key stays valid for reads until rotation completes (no-downtime).
    """
    _validate_fernet_key(req.new_fernet_key, "new_fernet_key")

    old_key = req.old_fernet_key or settings.fernet_key

    # Rotation runs cross-org on the modulo_system (BYPASSRLS) role. If that
    # role is not provisioned (MODULO_SYSTEM_DATABASE_URL empty) the system
    # session factory silently falls back to the NOBYPASSRLS app role, which
    # makes the rotation a zero-row no-op. Refuse loudly rather than
    # re-introduce the exact silent failure this fix addresses.
    if not settings.modulo_system_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Fernet key rotation is unavailable: the modulo_system role is "
                "not provisioned (MODULO_SYSTEM_DATABASE_URL is unset)."
            ),
        )

    # Cross-process atomic guard: Redis SET NX + TTL.  The asyncio.Lock
    # serialises concurrent callers within one process so two coroutines
    # don't race the Redis roundtrip, and the Redis key provides the
    # cross-process invariant.  The TTL self-heals if a crash prevents
    # release.
    async with _rotation_lock:
        # Fast in-memory check (avoids Redis roundtrip in the common case)
        if await _is_rotation_active(settings):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A key rotation is already in progress",
            )

        # Log the rotation start to audit log FIRST
        await append_audit_event(
            session,
            org_id=current_user.organisation_id,
            event_type="fernet_key_rotation_started",
            actor_user_id=current_user.account_id,
            resource_type="encryption",
            resource_id=current_user.organisation_id,
            payload_json={
                "initiated_by": str(current_user.account_id),
                "old_key_provided": bool(req.old_fernet_key),
            },
        )

        # Acquire the distributed lock (atomic SET NX + TTL).  The returned
        # owner token is threaded into the background task so its release only
        # deletes the lock if THIS rotation still owns it.
        lock_owner = await _acquire_rotation_lock(settings)
        if lock_owner is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A key rotation is already in progress",
            )

    # Launch background rotation task (lock is held by the Redis key, not
    # the asyncio.Lock — the asyncio.Lock scope has ended).
    task = asyncio.create_task(
        _run_rotation_background(
            new_key=req.new_fernet_key,
            old_key=old_key,
            org_id=current_user.organisation_id,
            actor_user_id=current_user.account_id,
            lock_owner=lock_owner,
        )
    )
    task_id = str(id(task))

    return RotateKeyResponse(
        status="accepted",
        task_id=task_id,
        message="Key rotation started — all encrypted data will be re-encrypted with the new key",
    )


@router.get(
    "/status",
    responses={
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("admin.rotation.rotation_status")
async def rotation_status(
    _current_user: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> RotationStatusResponse:
    """Return the current rotation state.

    Checks both the Redis distributed lock (cross-process) and the
    in-memory flag (fast path for same-process).
    """
    settings = get_settings()
    is_active = await _is_rotation_active(settings)
    return RotationStatusResponse(
        rotation_in_progress=is_active,
        last_rotation_result=_last_rotation_result,
    )


# ── Redis lock helpers ────────────────────────────────────────────────────


async def _is_rotation_active(settings: Settings) -> bool:
    """Check if rotation is in progress via Redis (cross-process safe).

    Falls back to False if Redis is unavailable (fail-open — a Redis blip
    should not wedge the status endpoint).
    """
    if not settings.redis_url:
        return False
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        try:
            exists = await r.exists(_ROTATION_LOCK_KEY)
            return bool(exists)
        finally:
            with contextlib.suppress(Exception):
                await r.aclose()
    except Exception:
        return False


async def _acquire_rotation_lock(settings: Settings) -> str | None:
    """Atomically acquire the rotation lock via Redis SET NX + TTL.

    Returns a unique owner token on success (pass it to
    :func:`_release_rotation_lock`), or ``None`` if the lock is already held.
    The owner token makes release discriminate owners: a rotation that
    outlives the TTL must not delete a successor's lock.

    Fails CLOSED if Redis is configured but unreachable (503): reporting a
    transient Redis blip as "already in progress" would be misleading, and
    proceeding without the lock would allow two concurrent rotations.
    """
    if not settings.redis_url:
        # No Redis — fall back to in-memory asyncio.Lock (single-process).
        # Return a token so the caller's success check is uniform.
        return uuid.uuid4().hex
    owner = uuid.uuid4().hex
    r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
    try:
        acquired = await r.set(
            _ROTATION_LOCK_KEY,
            owner,
            nx=True,
            ex=_ROTATION_LOCK_TTL_SECONDS,
        )
        return owner if acquired else None
    except Exception as exc:
        _log.exception("rotation.lock_acquire_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fernet key rotation is temporarily unavailable (lock store unreachable).",
        ) from exc
    finally:
        with contextlib.suppress(Exception):
            await r.aclose()


async def _release_rotation_lock(settings: Settings, owner: str) -> None:
    """Release the rotation lock key (best-effort, never raises).

    Only deletes the key if it still holds *owner* (our unique token), so a
    stale rotation cannot delete a successor's lock.  Uses a Lua script for
    atomicity.
    """
    if not settings.redis_url:
        return
    r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
    try:
        # Atomic: delete only if the key still holds OUR owner token.
        await r.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then   return redis.call('del', KEYS[1]) else   return 0 end",
            1,  # number of keys
            _ROTATION_LOCK_KEY,
            owner,  # owner token
        )
    except Exception:
        _log.exception("rotation.lock_release_failed")
    finally:
        with contextlib.suppress(Exception):
            await r.aclose()


# ── Background task ────────────────────────────────────────────────────────


async def _run_rotation_background(
    new_key: str,
    old_key: str,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    lock_owner: str,
) -> None:
    """Run the full rotation in the background and store the result.

    Rotation is inherently cross-org ("rotate all encrypted data"), so it runs on
    the ``modulo_system`` cross-org session factory (BYPASSRLS). The app role
    ``modulo_app`` is NOBYPASSRLS: on the org-scoped tables (``secrets``,
    ``connector_instances``, ``model_backends``, ``notification_endpoints``) the
    ``rls_org_isolation`` policy compares ``organisation_id`` against
    ``app.organisation_id``, which is empty here — so the UPDATEs fail-closed to
    ZERO rows and the rotation would silently no-op. The system factory bypasses
    RLS and is the same cross-org mechanism used by the retention/system crons.
    """
    global _last_rotation_result

    settings = get_settings()

    try:
        # Defensive guard: if the modulo_system role is unprovisioned, the system
        # factory silently falls back to the NOBYPASSRLS app role and the rotation
        # becomes a zero-row no-op (the exact bug this fix prevents). Refuse loudly
        # instead of reporting a hollow "completed" with 0 rows.
        if not settings.modulo_system_database_url:
            _log.error(
                "rotation.system_role_unprovisioned",
                extra={
                    "reason": (
                        "MODULO_SYSTEM_DATABASE_URL unset — refusing to rotate on the "
                        "NOBYPASSRLS app role (would silently no-op on RLS-scoped tables)"
                    )
                },
            )
            _last_rotation_result = {
                "status": "failed",
                "error": (
                    "modulo_system role unprovisioned (MODULO_SYSTEM_DATABASE_URL unset); "
                    "rotation refused to avoid a silent no-op."
                ),
            }
            return

        async with _make_system_session_factory()() as session, session.begin():
            result = await rotate_all_encrypted_data(session, new_key, old_key)

            # Log completion inside the transaction so it gets committed
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="fernet_key_rotation_completed",
                actor_user_id=actor_user_id,
                resource_type="encryption",
                resource_id=org_id,
                payload_json={
                    "tables_processed": result.tables_processed,
                    "total_rows_reencrypted": result.total_rows_reencrypted,
                },
            )

            _last_rotation_result = {
                "status": "completed",
                "tables_processed": result.tables_processed,
                "total_rows_reencrypted": result.total_rows_reencrypted,
                "details": result.details,
            }

        _log.info(
            "rotation.completed",
            extra={
                "tables": result.tables_processed,
                "total_rows": result.total_rows_reencrypted,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("rotation.failed")
        _last_rotation_result = {
            "status": "failed",
            "error": str(exc),
        }
    finally:
        # Always release the Redis lock — covers success, failure, and crash
        # recovery.  The TTL already provides self-healing, but releasing
        # eagerly avoids leaving a stale lock for the full TTL window.  The
        # owner token guards against deleting a successor's lock if this
        # rotation outlived the TTL.
        await _release_rotation_lock(settings, lock_owner)
