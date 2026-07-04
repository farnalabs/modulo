"""ModelBackend CRUD REST API.

Credentials (API keys) are encrypted at rest with Fernet. The ciphertext is
never exposed in any response — only a boolean `has_credentials` field.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.model_backend import (
    create_model_backend,
    delete_model_backend,
    get_model_backend,
    list_model_backends,
    update_model_backend,
)
from modulo.db.models.model_backend import ModelBackend
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.core.plugin_registry import get_plugin_registry
from modulo.settings import Settings, get_settings
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

router = APIRouter(prefix="/api/v1/model-backends", tags=["model-backends"])


def _encrypt(api_key: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode()).encrypt(api_key.encode())


class ModelBackendCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(..., min_length=1, max_length=128)
    model_id: str = Field(..., min_length=1, max_length=128)
    api_key: str = Field(..., min_length=1)
    default_params: dict[str, Any] = {}
    visibility: str = Field(default="org")
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] = Field(default="native")


class ModelBackendUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    display_name: str | None = Field(None, min_length=1, max_length=255)
    model_id: str | None = Field(None, min_length=1, max_length=128)
    api_key: str | None = Field(None, min_length=1)
    default_params: dict[str, Any] | None = None
    visibility: str | None = None
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] | None = None


class ModelBackendResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    display_name: str
    provider: str
    model_id: str
    has_credentials: bool
    default_params: dict[str, Any]
    visibility: str
    tier: str
    fallback_backend_ids: list[uuid.UUID] | None = None
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": False}


class ModelBackendListResponse(BaseModel):
    items: list[ModelBackendResponse]
    total: int
    page: int
    page_size: int


def _to_response(mb: Any) -> ModelBackendResponse:
    raw_fallback_ids = getattr(mb, "fallback_backend_ids", None)
    fallback_ids: list[uuid.UUID] | None = None
    if raw_fallback_ids:
        fallback_ids = [uuid.UUID(fid) if isinstance(fid, str) else fid for fid in raw_fallback_ids]
    return ModelBackendResponse(
        id=mb.id,
        organisation_id=mb.organisation_id,
        name=mb.name,
        display_name=mb.display_name,
        provider=mb.provider,
        model_id=mb.model_id,
        has_credentials=bool(mb.credentials_ciphertext),
        default_params=mb.default_params,
        visibility=mb.visibility,
        tier=mb.tier,
        fallback_backend_ids=fallback_ids,
        account_id=mb.account_id,
        created_at=mb.created_at,
        updated_at=mb.updated_at,
    )


@router.get("", response_model=ModelBackendListResponse)
async def list_model_backends_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ModelBackendListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_model_backends(session, page=page, page_size=page_size)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    return ModelBackendListResponse(
        items=[_to_response(mb) for mb in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


_VALID_PROVIDERS = {
    "ai21", "anthropic", "azure_openai", "bedrock", "cohere", "deepseek",
    "fireworks", "gemini", "grok", "groq", "jan", "llamacpp", "lm_studio",
    "localai", "mistral", "ollama", "openai", "openrouter", "perplexity",
    "qwen", "tgi", "togetherai", "vertexai", "vllm", "watsonx",
}


def _validate_provider(provider: str) -> None:
    """Raise 422 if provider is not a known built-in or plugin backend."""
    if provider in _VALID_PROVIDERS:
        return
    try:
        registry = get_plugin_registry()
        if registry.has_model_backend(provider):
            return
    except Exception:
        pass
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=[
            {
                "type": "value_error",
                "loc": ["body", "provider"],
                "msg": f"Unknown model backend provider: {provider!r}",
            }
        ],
    )


@router.post("", response_model=ModelBackendResponse, status_code=status.HTTP_201_CREATED)
async def create_model_backend_endpoint(
    body: ModelBackendCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    _validate_provider(body.provider)
    ciphertext = _encrypt(body.api_key, settings.fernet_key)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            existing = (
                await session.execute(
                    select(ModelBackend).where(
                        ModelBackend.organisation_id == principal.organisation_id,
                        ModelBackend.name == body.name,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A model backend with name {body.name!r} already exists in this organisation",
                )

            fallback_ids: list[str] | None = None
            if body.fallback_backend_ids:
                fallback_ids = [str(fid) for fid in body.fallback_backend_ids]
            mb = await create_model_backend(
                session,
                org_id=principal.organisation_id,
                name=body.name,
                display_name=body.display_name,
                provider=body.provider,
                model_id=body.model_id,
                credentials_ciphertext=ciphertext,
                account_id=principal.account_id,
                default_params=body.default_params,
                visibility=body.visibility,
                fallback_backend_ids=fallback_ids,
                tier=body.tier,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    return _to_response(mb)


@router.get("/{backend_id}", response_model=ModelBackendResponse)
async def get_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ModelBackendResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    if mb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
    return _to_response(mb)


@router.patch("/{backend_id}", response_model=ModelBackendResponse)
async def update_model_backend_endpoint(
    backend_id: uuid.UUID,
    body: ModelBackendUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    updates: dict[str, Any] = {k: v for k, v in body.model_dump().items() if v is not None}
    if "api_key" in updates:
        _ct = _encrypt(updates.pop("api_key"), settings.fernet_key)
        updates["credentials_ciphertext"] = _ct  # nosemgrep: credential-not-in-state
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await update_model_backend(session, backend_id, updates)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    if mb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
    return _to_response(mb)


@router.delete("/{backend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_model_backend(session, backend_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
