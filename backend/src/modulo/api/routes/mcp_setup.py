"""API routes for MCP setup handoff completion."""

import logging
import uuid
from typing import Annotated, Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.mcp_setup_handoff import consume_handoff
from modulo.db.crud.model_backend import get_model_backend, update_model_backend
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["mcp-setup"])


class CompleteSetupRequest(BaseModel):
    token: str = Field(..., min_length=1, description="One-time setup token from the MCP tool response")
    api_key: str = Field(..., min_length=1, description="The API key to configure")


@router.post("/model-backends/{backend_id}/complete-setup", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("mcp_setup.complete_model_backend_setup")
async def complete_model_backend_setup(
    backend_id: uuid.UUID,
    payload: CompleteSetupRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
) -> dict[str, Any]:
    """Complete the setup of a model backend by providing the API key via browser."""
    settings = get_settings()
    org_id = principal.organisation_id

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            record = await consume_handoff(
                session,
                raw_token=payload.token,
                resource_type="model-backend",
                org_id=org_id,
            )
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "invalid_token", "detail": "Token not found, expired, or already used"},
                )

            if str(record.resource_id) != str(backend_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "token_mismatch", "detail": "Token does not match the specified backend"},
                )

            existing = await get_model_backend(session, backend_id)
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "backend_not_found", "backend_id": str(backend_id)},
                )
            if existing.status != "pending_setup":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "already_configured", "detail": "Backend is already configured"},
                )

            fernet_key = settings.fernet_key
            if not fernet_key:
                _log.error("FERNET_KEY is not configured")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error": "encryption_config_error", "detail": "Encryption is not configured"},
                )

            try:
                fernet = Fernet(fernet_key.encode())
            except (InvalidToken, ValueError, TypeError) as exc:
                _log.exception("Failed to initialise Fernet: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error": "encryption_error", "detail": "Failed to initialise encryption"},
                ) from exc

            try:
                ciphertext = fernet.encrypt(payload.api_key.encode())
            except Exception as exc:
                _log.exception("mcp_setup.complete_model_backend_setup: failed to encrypt API key")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error": "encryption_error", "detail": "Failed to encrypt API key"},
                ) from exc

            updates = {
                "credentials_ciphertext": ciphertext,
                "status": "active",
            }
            updated = await update_model_backend(session, backend_id, updates)

        if updated is None:
            _log.error("Failed to update model backend %s: returned None", backend_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "update_failed", "detail": "Failed to update model backend"},
            )

        return {
            "status": "ok",
            "backend_id": str(updated.id),
            "name": updated.name,
        }
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _log.exception("mcp_setup.complete_model_backend_setup: database error for backend %s", backend_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "database_error", "detail": "A database error occurred"},
        ) from exc
    except Exception as exc:
        _log.exception("mcp_setup.complete_model_backend_setup: unexpected error for backend %s", backend_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "internal_error", "detail": "An unexpected error occurred"},
        ) from exc
