"""Admin API endpoints for Fernet key rotation."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
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

_CODE_ADMIN_ROTATION_ROTATE_KEY = "admin_rotation.rotate_key"
_CODE_ADMIN_ROTATION_ROTATION_STATUS = "admin_rotation.rotation_status"


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
    try:
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
        except HTTPException:
            raise
        except Exception:
            _log.exception("Failed to record fernet_key_rotation_started audit event")
            raise HTTPException(status_code=500, detail="Internal server error.") from None

        _rotation_in_progress = True

        import asyncio

        # Launch background rotation task.
        task = asyncio.create_task(
            _run_rotation_background(
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
    except HTTPException:
        raise
    except IntegrityError as exc:
        _log.exception(_CODE_ADMIN_ROTATION_ROTATE_KEY)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from exc
    except ProgrammingError as exc:
        _log.exception(_CODE_ADMIN_ROTATION_ROTATE_KEY)
        raise HTTPException(status_code=503, detail="Database not available. Run migrations.") from exc
    except Exception as e:
        _log.exception(_CODE_ADMIN_ROTATION_ROTATE_KEY)
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from e


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
    """Return the current rotation state."""
    try:
        return RotationStatusResponse(
            rotation_in_progress=_rotation_in_progress,
            last_rotation_result=_last_rotation_result,
        )
    except HTTPException:
        raise
    except IntegrityError as exc:
        _log.exception(_CODE_ADMIN_ROTATION_ROTATION_STATUS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from exc
    except ProgrammingError as exc:
        _log.exception(_CODE_ADMIN_ROTATION_ROTATION_STATUS)
        raise HTTPException(status_code=503, detail="Database not available. Run migrations.") from exc
    except Exception as e:
        _log.exception(_CODE_ADMIN_ROTATION_ROTATION_STATUS)
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from e


# ── Background task ────────────────────────────────────────────────────────


async def _run_rotation_background(
    new_key: str,
    old_key: str,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
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
    global _rotation_in_progress, _last_rotation_result

    # Defensive guard: if the modulo_system role is unprovisioned, the system
    # factory silently falls back to the NOBYPASSRLS app role and the rotation
    # becomes a zero-row no-op (the exact bug this fix prevents). Refuse loudly
    # instead of reporting a hollow "completed" with 0 rows.
    settings = get_settings()
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
        _rotation_in_progress = False
        return

    try:
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
        _rotation_in_progress = False
