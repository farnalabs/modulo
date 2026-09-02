"""EnvironmentProfile CRUD + sandbox test REST API (v1)."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.runtime_provider import RuntimeProvider, create_default_hub
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.db.crud.environment_profile import (
    create_environment_profile,
    get_environment_profile,
    list_environment_profiles,
    restore_environment_profile,
    soft_delete_environment_profile,
    update_environment_profile,
)
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.rls import set_rls_org, set_rls_user_context

_MSG_DATABASE_ERROR_OCCURRED_PLEASE = "Database error occurred. Please try again later."
_CODE_ENVIRONMENT_PROFILES_CREATE_PROFILE = "environment_profiles.create_profile"
_MSG_ENVIRONMENT_PROFILE_NOT_FOUND = "Environment profile not found"
_CODE_ENVIRONMENT_PROFILES_UPDATE_PROFILE = "environment_profiles.update_profile"
_CODE_ENVIRONMENT_PROFILES_TEST_PROFILE = "environment_profiles.test_profile"


_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/environment-profiles",
    tags=["environment-profiles"],
    dependencies=[require_feature("environment_profiles")],
)


@lru_cache
def _get_hub() -> RuntimeProviderHub:
    """Process-global RuntimeProviderHub singleton.

    ``lru_cache`` ensures the hub is created once and reused across all
    requests.  The E2B provider is auto-registered when
    ``MODULO_E2B_API_KEY`` is set — adding the key post-deployment and
    restarting the process is enough to switch from local to sandboxed
    execution.
    """
    from modulo.settings import get_settings

    settings = get_settings()
    return create_default_hub(max_local_concurrency=settings.modulo_max_local_concurrency)


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    provider_type: str = Field(default="local_docker")
    image_ref: str | None = Field(None, min_length=1, max_length=500)
    capabilities: list[str] = Field(default_factory=list)
    config_json: dict[str, Any] = Field(default_factory=dict)
    network_policy: str = Field(default="outbound")
    initialisation_strategy: str = Field(default="git_clone")
    secret_refs: list[str] = Field(default_factory=list)
    persistence_policy: str = Field(default="ephemeral")
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org")


class ProfileUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    provider_type: str | None = None
    image_ref: str | None = Field(None, min_length=1, max_length=500)
    capabilities: list[str] | None = None
    config_json: dict[str, Any] | None = None
    network_policy: str | None = None
    initialisation_strategy: str | None = None
    secret_refs: list[str] | None = None
    persistence_policy: str | None = None
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = None


class ProfileResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None = None
    provider_type: str
    image_ref: str | None = None
    capabilities: list[str]
    config_json: dict[str, Any]
    network_policy: str
    initialisation_strategy: str
    secret_refs: list[str]
    persistence_policy: str
    status: str
    owner_team_id: uuid.UUID | None = None
    visibility: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ProfileListResponse(BaseModel):
    items: list[ProfileResponse]
    total: int
    page: int
    page_size: int


def _to_response(p: EnvironmentProfile) -> ProfileResponse:
    return ProfileResponse(
        id=p.id,
        organisation_id=p.organisation_id,
        name=p.name,
        description=p.description,
        provider_type=p.provider_type,
        image_ref=p.image_ref,
        capabilities=p.capabilities_json,
        config_json=p.config_json,
        network_policy=p.network_policy,
        initialisation_strategy=p.initialisation_strategy,
        secret_refs=p.secret_refs_json,
        persistence_policy=p.persistence_policy,
        status=p.status,
        owner_team_id=p.owner_team_id,
        visibility=p.visibility,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("")
@handle_db_errors("environment_profiles.list_profiles")
async def list_profiles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.list"),
) -> ProfileListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_environment_profiles(session, page=page, page_size=page_size)
    except ProgrammingError:
        _log.exception("environment_profiles.list_profiles")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("environment_profiles.list_profiles")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error listing environment profiles: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return ProfileListResponse(
        items=[_to_response(p) for p in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors(_CODE_ENVIRONMENT_PROFILES_CREATE_PROFILE)
async def create_profile(
    req: ProfileCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.create"),
) -> ProfileResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await create_environment_profile(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                provider_type=req.provider_type,
                image_ref=req.image_ref,
                capabilities=req.capabilities,
                config_json=req.config_json,
                network_policy=req.network_policy,
                initialisation_strategy=req.initialisation_strategy,
                secret_refs=req.secret_refs,
                persistence_policy=req.persistence_policy,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
            )
    except IntegrityError:
        _log.exception(_CODE_ENVIRONMENT_PROFILES_CREATE_PROFILE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An environment profile with this name already exists.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_ENVIRONMENT_PROFILES_CREATE_PROFILE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_ENVIRONMENT_PROFILES_CREATE_PROFILE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error creating environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return _to_response(profile)


@router.get("/{profile_id}")
@handle_db_errors("environment_profiles.get_profile")
async def get_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.list"),
) -> ProfileResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await get_environment_profile(session, profile_id)
    except ProgrammingError:
        _log.exception("environment_profiles.get_profile")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("environment_profiles.get_profile")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception as exc:
        _log.exception("Unexpected error fetching environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ENVIRONMENT_PROFILE_NOT_FOUND)
    return _to_response(profile)


@router.put("/{profile_id}")
@handle_db_errors(_CODE_ENVIRONMENT_PROFILES_UPDATE_PROFILE)
async def update_profile(
    profile_id: uuid.UUID,
    req: ProfileUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.update"),
) -> ProfileResponse:
    updates = req.model_dump(exclude_unset=True)
    if "capabilities" in updates:
        updates["capabilities_json"] = updates.pop("capabilities")
    if "secret_refs" in updates:
        updates["secret_refs_json"] = updates.pop("secret_refs")
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await update_environment_profile(session, profile_id, updates)
    except IntegrityError:
        _log.exception(_CODE_ENVIRONMENT_PROFILES_UPDATE_PROFILE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An environment profile with this name already exists.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_ENVIRONMENT_PROFILES_UPDATE_PROFILE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_ENVIRONMENT_PROFILES_UPDATE_PROFILE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error updating environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ENVIRONMENT_PROFILE_NOT_FOUND)
    return _to_response(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("environment_profiles.delete_profile")
async def delete_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.delete"),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await soft_delete_environment_profile(session, profile_id)
    except ProgrammingError:
        _log.exception("environment_profiles.delete_profile")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("environment_profiles.delete_profile")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception as exc:
        _log.exception("Unexpected error deleting environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ENVIRONMENT_PROFILE_NOT_FOUND)


@router.post("/{profile_id}/restore")
@handle_db_errors("environment_profiles.restore_profile")
async def restore_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.create"),
) -> ProfileResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await restore_environment_profile(session, profile_id)
    except ProgrammingError:
        _log.exception("environment_profiles.restore_profile")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("environment_profiles.restore_profile")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error restoring environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ENVIRONMENT_PROFILE_NOT_FOUND)
    return _to_response(profile)


# ---------------------------------------------------------------------------
# Sandbox test endpoint
# ---------------------------------------------------------------------------


async def _get_profile_or_404(session: AsyncSession, profile_id: uuid.UUID) -> EnvironmentProfile:
    profile = await get_environment_profile(session, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_ENVIRONMENT_PROFILE_NOT_FOUND,
        )
    return profile


def _sse_event(event: str, detail: str) -> str:
    data = json.dumps(
        {
            "event": event,
            "detail": detail,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    return f"data: {data}\n\n"


def _build_workspace_spec(profile: EnvironmentProfile) -> Any:
    from modulo.core.runtime_provider import WorkspaceSpec

    cfg = profile.config_json or {}
    return WorkspaceSpec(
        environment_profile_id=profile.id,
        organisation_id=profile.organisation_id,
        run_id=None,
        image_ref=profile.image_ref or "",
        capabilities=profile.capabilities_json or [],
        timeout_seconds=cfg.get("timeout_seconds", 3600),
        resource_limits=cfg,
        egress_policy=profile.network_policy or "deny_all",
        persistence_policy={"strategy": profile.persistence_policy},
        labels={"profile_name": profile.name},
    )


async def _sandbox_test_stream(profile: EnvironmentProfile) -> AsyncIterator[str]:
    """Stream sandbox lifecycle events as SSE."""
    provider_ref: str | None = None
    provider: RuntimeProvider | None = None

    try:
        yield _sse_event("provisioning", "Creating sandbox...")
        await asyncio.sleep(0.5)

        hub = _get_hub()
        provider = hub.resolve(profile) or hub.get("local")
        if provider is None:
            yield _sse_event("failed", "No RuntimeProvider available — check server configuration")
            return

        spec = _build_workspace_spec(profile)
        provider_ref = await provider.create_workspace(spec)
        yield _sse_event("provisioned", f"Workspace created via {type(provider).__name__}: {provider_ref}")
        await asyncio.sleep(0.3)

        yield _sse_event("command_start", 'Executing: echo "Hello from Modulo sandbox"')
        result = await provider.exec_command(provider_ref, ["echo", "Hello from Modulo sandbox"], cmd_timeout=30)
        yield _sse_event(
            "command_complete",
            json.dumps(
                {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                }
            ),
        )
        await asyncio.sleep(0.3)

        yield _sse_event("destroying", "Destroying sandbox...")
        await provider.destroy_workspace(provider_ref)
        yield _sse_event("destroyed", "Sandbox destroyed successfully")
    except HTTPException:
        raise
    except Exception:
        _log.exception("Sandbox test failed for profile %s", profile.id)
        yield _sse_event("failed", "Test failed — check server logs for details")
        if provider_ref and provider is not None:
            try:
                await provider.destroy_workspace(provider_ref)
            except HTTPException:
                raise
            except Exception:
                _log.warning("Failed to clean up sandbox %s after error", provider_ref)


@router.post("/{profile_id}/test")
@handle_db_errors(_CODE_ENVIRONMENT_PROFILES_TEST_PROFILE)
async def test_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("environment_profile.test"),
) -> StreamingResponse:
    """Provision a sandbox from the profile, run echo, destroy it — stream events."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        profile = await _get_profile_or_404(session, profile_id)
    return StreamingResponse(
        _sandbox_test_stream(profile),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
