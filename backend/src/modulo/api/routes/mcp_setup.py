"""API routes for MCP setup handoff completion."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.mcp_setup_handoff import consume_handoff
from modulo.db.crud.model_backend import get_model_backend, update_model_backend
from modulo.settings import get_settings

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
    from cryptography.fernet import Fernet

    settings = get_settings()
    org_id = principal.organisation_id

    # Consume the handoff token
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

    # Encrypt the API key
    fernet = Fernet(settings.fernet_key.encode())
    ciphertext = fernet.encrypt(body.api_key.encode())

    # Update the backend
    updates = {
        "credentials_ciphertext": ciphertext,
        "status": "active",
    }
    updated = await update_model_backend(session, backend_id, updates)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "backend_not_found", "backend_id": str(backend_id)},
        )

    return {
        "status": "ok",
        "backend_id": str(updated.id),
        "name": updated.name,
    }
