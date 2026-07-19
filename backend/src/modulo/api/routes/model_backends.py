"""ModelBackend CRUD REST API.

Credentials (API keys) are encrypted at rest with Fernet. The ciphertext is
never exposed in any response — only a boolean `has_credentials` field.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, ClassVar, Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.plugin_registry import get_plugin_registry
from modulo.db.crud.model_backend import (
    create_model_backend,
    delete_model_backend,
    get_model_backend,
    list_model_backends,
    update_model_backend,
)
from modulo.db.models.model_backend import ModelBackend
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/model-backends", tags=["model-backends"])


def _encrypt(api_key: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode()).encrypt(api_key.encode())


class ModelBackendCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(..., min_length=1, max_length=128)
    model_id: str = Field(..., min_length=1, max_length=128)
    api_key: str = Field(..., min_length=1)
    default_params: ClassVar[dict[str, Any]] = {}
    visibility: str = Field(default="org")
    owner_team_id: uuid.UUID | None = None
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] = Field(default="native")

    @model_validator(mode="after")
    def _validate_team_visibility(self) -> "ModelBackendCreate":
        if self.visibility == "team" and self.owner_team_id is None:
            raise ValueError("owner_team_id is required when visibility is 'team'")
        return self


class ModelBackendUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    display_name: str | None = Field(None, min_length=1, max_length=255)
    model_id: str | None = Field(None, min_length=1, max_length=128)
    api_key: str | None = Field(None, min_length=1)
    default_params: dict[str, Any] | None = None
    visibility: str | None = None
    owner_team_id: uuid.UUID | None = None
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] | None = None

    @model_validator(mode="after")
    def _validate_team_visibility(self) -> "ModelBackendUpdate":
        if self.visibility == "team" and self.owner_team_id is None:
            raise ValueError("owner_team_id is required when visibility is 'team'")
        return self


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
    owner_team_id: uuid.UUID | None = None
    tier: str
    fallback_backend_ids: list[uuid.UUID] | None = None
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ModelBackendListResponse(BaseModel):
    items: list[ModelBackendResponse]
    total: int
    page: int
    page_size: int


def _to_response(mb: Any) -> ModelBackendResponse:
    raw_fallback_ids = getattr(mb, "fallback_backend_ids", None)
    fallback_ids: list[uuid.UUID] | None = None
    if raw_fallback_ids is not None:
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
        owner_team_id=mb.owner_team_id,
        tier=mb.tier,
        fallback_backend_ids=fallback_ids,
        created_by=mb.account_id,
        created_at=mb.created_at,
        updated_at=mb.updated_at,
    )


@handle_db_errors("model_backends.list_model_backends_endpoint")
@router.get("", response_model=ModelBackendListResponse)
async def list_model_backends_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ModelBackendListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_model_backends(
                session, org_id=principal.organisation_id, page=page, page_size=page_size
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing model backends.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error listing model backends: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing model backends.",
        ) from None
    return ModelBackendListResponse(
        items=[_to_response(mb) for mb in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


_VALID_PROVIDERS = {
    "ai21",
    "anthropic",
    "azure_openai",
    "bedrock",
    "cohere",
    "custom",
    "deepseek",
    "fireworks",
    "gemini",
    "grok",
    "groq",
    "jan",
    "llamacpp",
    "lm_studio",
    "localai",
    "mistral",
    "ollama",
    "opencode",
    "openai",
    "openrouter",
    "perplexity",
    "qwen",
    "replicate",
    "tgi",
    "togetherai",
    "vertexai",
    "vllm",
    "watsonx",
}


def _validate_provider(provider: str) -> None:
    """Raise 422 if provider is not a known built-in or plugin backend."""
    if provider in _VALID_PROVIDERS:
        return
    try:
        registry = get_plugin_registry()
        if registry.has_model_backend(provider):
            return
    except Exception as exc:
        logger.warning("Plugin registry check failed for provider %r: %s", provider, exc)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[
            {
                "type": "value_error",
                "loc": ["body", "provider"],
                "msg": f"Unknown model backend provider: {provider!r}",
            }
        ],
    )


@handle_db_errors("model_backends.create_model_backend_endpoint")
@router.post("", response_model=ModelBackendResponse, status_code=status.HTTP_201_CREATED)
async def create_model_backend_endpoint(
    req: ModelBackendCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    _validate_provider(req.provider)
    ciphertext = _encrypt(req.api_key, settings.fernet_key)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            existing = (
                await session.execute(
                    select(ModelBackend)
                    .where(
                        ModelBackend.organisation_id == principal.organisation_id,
                        ModelBackend.name == req.name,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A model backend with name {req.name!r} already exists in this organisation",
                )

            fallback_ids: list[str] | None = None
            if req.fallback_backend_ids:
                fallback_ids = [str(fid) for fid in req.fallback_backend_ids]
            mb = await create_model_backend(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                display_name=req.display_name,
                provider=req.provider,
                model_id=req.model_id,
                credentials_ciphertext=ciphertext,
                account_id=principal.account_id,
                default_params=req.default_params,
                visibility=req.visibility,
                owner_team_id=req.owner_team_id,
                fallback_backend_ids=fallback_ids,
                tier=req.tier,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while creating model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error creating model backend: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating model backend.",
        ) from None
    import json

    from modulo.core.secrets_backend import create_secrets_backend

    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    secret_value = json.dumps({"api_key": req.api_key})
    await secrets_backend.set_secret(str(mb.id), secret_value)
    return _to_response(mb)


@handle_db_errors("model_backends.get_model_backend_endpoint")
@router.get("/{backend_id}", response_model=ModelBackendResponse)
async def get_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ModelBackendResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error fetching model backend: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching model backend.",
        ) from None
    if mb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
    return _to_response(mb)


@handle_db_errors("model_backends.update_model_backend_endpoint")
@router.patch("/{backend_id}", response_model=ModelBackendResponse)
async def update_model_backend_endpoint(
    backend_id: uuid.UUID,
    req: ModelBackendUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    updates: dict[str, Any] = req.model_dump(exclude_unset=True)
    if "api_key" in updates and updates["api_key"] is not None:
        _ct = _encrypt(updates.pop("api_key"), settings.fernet_key)
        updates["credentials_ciphertext"] = _ct  # nosemgrep: credential-not-in-state
    elif "api_key" in updates:
        updates.pop("api_key")
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await update_model_backend(session, backend_id, updates)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error updating model backend: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating model backend.",
        ) from None
    if mb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
    if req.api_key is not None:
        import json

        from modulo.core.secrets_backend import create_secrets_backend

        secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
        secret_value = json.dumps({"api_key": req.api_key})
        await secrets_backend.set_secret(str(mb.id), secret_value)
    return _to_response(mb)


@handle_db_errors("model_backends.delete_model_backend_endpoint")
@router.delete("/{backend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_model_backend(session, backend_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while deleting model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error deleting model backend: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting model backend.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
