"""API routes for MCP setup handoff completion."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.mcp_setup_handoff import consume_handoff
from modulo.db.crud.model_backend import get_model_backend, update_model_backend
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["mcp-setup"])


class CompleteSetupRequest(BaseModel):
    token: str = Field(..., description="One-time setup token from the MCP tool response")
    api_key: str = Field(..., description="The API key to configure")


@router.post("/model-backends/{backend_id}/complete-setup")
async def complete_model_backend_setup(
    backend_id: uuid.UUID,
    body: CompleteSetupRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict:
    """Complete the setup of a model backend by providing the API key via browser."""
    from cryptography.fernet import Fernet, InvalidToken

    settings = get_settings()
    org_id = principal.organisation_id

    await set_rls_org(session, org_id)

    async with session.begin():
        record = await consume_handoff(
            session,
            raw_token=body.token,
            resource_type="model-backend",
            org_id=org_id,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "invalid_token", "detail": "Token not found, expired, or already used"},
            )

        if record.resource_id != backend_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "token_mismatch", "detail": "Token does not match the specified backend"},
            )

        # Verify the backend is still in pending_setup
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

        # Encrypt the API key
        try:
            fernet = Fernet(settings.fernet_key.encode())
        except (InvalidToken, ValueError, TypeError) as exc:
            _log.error("Failed to initialise Fernet: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "encryption_error", "detail": "Failed to initialise encryption"},
            ) from exc

        ciphertext = fernet.encrypt(body.api_key.encode())

        updates = {
            "credentials_ciphertext": ciphertext,
            "status": "active",
        }
        updated = await update_model_backend(session, backend_id, updates)

    return {
        "status": "ok",
        "backend_id": str(updated.id),
        "name": updated.name,
    }
