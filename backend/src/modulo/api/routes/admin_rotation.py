"""Admin API endpoints for Fernet key rotation."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.api.dependencies import get_db_session, get_or_create_engine, get_or_create_session_factory
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.settings import Settings, get_settings

_MIN_KEY_LEN = 32

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/rotation", tags=["admin-rotation"])

# ── In-memory rotation state ──────────────────────────────────────────────

_rotation_in_progress: bool = False
_last_rotation_result: dict[str, Any] | None = None


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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} must be at least {_MIN_KEY_LEN} bytes; got {len(key.encode())}",
        )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/rotate-key", response_model=RotateKeyResponse, status_code=status.HTTP_202_ACCEPTED)
async def rotate_key(
    req: RotateKeyRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RotateKeyResponse:
    """Start a Fernet key rotation.

    Re-encrypts all Fernet-encrypted data across all stores with the new key.
    The old key stays valid for reads until rotation completes (no-downtime).
    """
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can rotate keys",
        )

    _validate_fernet_key(req.new_fernet_key, "new_fernet_key")

    old_key = req.old_fernet_key or settings.fernet_key

    global _rotation_in_progress
    if _rotation_in_progress:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A key rotation is already in progress",
        )

    # Log the rotation start to audit log FIRST
    try:
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
    except Exception:
        _log.exception("Failed to record fernet_key_rotation_started audit event")
        raise

    _rotation_in_progress = True

    # Launch background rotation task.
    # We use the global engine/session factory to avoid re-creating connections.
    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    import asyncio

    task = asyncio.create_task(
        _run_rotation_background(
            factory=factory,
            new_key=req.new_fernet_key,
            old_key=old_key,
            org_id=current_user.organisation_id,
            actor_user_id=current_user.account_id,
        )
    )
    task_id = str(id(task))

    return RotateKeyResponse(
        status="accepted",
        task_id=task_id,
        message="Key rotation started — all encrypted data will be re-encrypted with the new key",
    )


@router.get("/status", response_model=RotationStatusResponse)
async def rotation_status(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> RotationStatusResponse:
    """Return the current rotation state."""
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can view rotation status",
        )

    return RotationStatusResponse(
        rotation_in_progress=_rotation_in_progress,
        last_rotation_result=_last_rotation_result,
    )


# ── Background task ────────────────────────────────────────────────────────


async def _run_rotation_background(
    factory: async_sessionmaker[AsyncSession],
    new_key: str,
    old_key: str,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    """Run the full rotation in the background and store the result."""
    global _rotation_in_progress, _last_rotation_result

    try:
        async with factory() as session, session.begin():
            from modulo.core.fernet_rotation import rotate_all_encrypted_data

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
    except Exception as exc:
        _log.exception("rotation.failed")
        _last_rotation_result = {
            "status": "failed",
            "error": str(exc),
        }
    finally:
        _rotation_in_progress = False
