"""EnvironmentProfile CRUD + sandbox test REST API."""

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
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.runtime_provider import RuntimeProvider, create_default_hub
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.db.crud.environment_profile import (
    create_environment_profile,
    delete_environment_profile,
    get_environment_profile,
    list_environment_profiles,
    update_environment_profile,
)
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/environments", tags=["environments"])


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

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    image_ref: str = Field(..., min_length=1, max_length=500)
    capabilities: list[str] = Field(default_factory=list)
    egress_policy: str | None = Field(None, pattern=r"^(deny_all|allow_all|allow_listed)$")
    timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    persistence_policy: dict[str, Any] = Field(default_factory=dict)


class ProfileUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    image_ref: str | None = Field(None, min_length=1, max_length=500)
    capabilities: list[str] | None = None
    egress_policy: str | None = Field(None, pattern=r"^(deny_all|allow_all|allow_listed)$")
    timeout_seconds: int | None = Field(None, ge=60, le=86400)
    resource_limits: dict[str, Any] | None = None
    persistence_policy: dict[str, Any] | None = None
    is_active: bool | None = None


class ProfileResponse(BaseModel):
    id: str
    organisation_id: str
    name: str
    description: str | None
    image_ref: str
    capabilities: list[str]
    egress_policy: str | None
    timeout_seconds: int
    resource_limits: dict[str, Any]
    persistence_policy: dict[str, Any]
    is_active: bool
    created_by: str | None
    created_at: str | None
    updated_at: str | None

    model_config = {"from_attributes": True}


class ProfileListResponse(BaseModel):
    items: list[ProfileResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(p: EnvironmentProfile) -> ProfileResponse:
    return ProfileResponse(
        id=str(p.id),
        organisation_id=str(p.organisation_id),
        name=p.name,
        description=p.description,
        image_ref=p.image_ref,
        capabilities=p.capabilities,
        egress_policy=p.egress_policy,
        timeout_seconds=p.timeout_seconds,
        resource_limits=p.resource_limits_json,
        persistence_policy=p.persistence_policy,
        is_active=p.is_active,
        created_by=str(p.account_id) if p.account_id else None,
        created_at=p.created_at.isoformat() if p.created_at else None,
        updated_at=p.updated_at.isoformat() if p.updated_at else None,
    )


async def _get_profile_or_404(session: AsyncSession, profile_id: uuid.UUID) -> EnvironmentProfile:
    profile = await get_environment_profile(session, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Environment profile not found",
        )
    return profile


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ProfileListResponse)
async def list_profiles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ProfileListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await list_environment_profiles(session, page=page, page_size=page_size)
    return ProfileListResponse(
        items=[_to_response(p) for p in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: ProfileCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ProfileResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        profile = await create_environment_profile(
            session,
            org_id=principal.organisation_id,
            name=body.name,
            image_ref=body.image_ref,
            created_by=principal.account_id,
            description=body.description,
            capabilities=body.capabilities,
            egress_policy=body.egress_policy,
            timeout_seconds=body.timeout_seconds,
            resource_limits=body.resource_limits,
            persistence_policy=body.persistence_policy,
        )
    return _to_response(profile)


@router.get("/{profile_id}", response_model=ProfileResponse)
async def get_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ProfileResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        profile = await _get_profile_or_404(session, profile_id)
    return _to_response(profile)


@router.patch("/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: uuid.UUID,
    body: ProfileUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ProfileResponse:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "resource_limits" in updates:
        updates["resource_limits_json"] = updates.pop("resource_limits")
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        profile = await update_environment_profile(session, profile_id, updates)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Environment profile not found",
        )
    return _to_response(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        deleted = await delete_environment_profile(session, profile_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Environment profile not found",
        )


# ---------------------------------------------------------------------------
# Sandbox test endpoint
# ---------------------------------------------------------------------------


class TestStepEvent(BaseModel):
    event: str
    detail: str
    timestamp: str


async def _sandbox_test_stream(profile: EnvironmentProfile) -> AsyncIterator[str]:
    """Stream sandbox lifecycle events as SSE."""
    provider_ref: str | None = None

    try:
        # Provisioning
        yield _sse_event("provisioning", "Creating sandbox...")
        await asyncio.sleep(0.5)

        hub = _get_hub()
        provider: RuntimeProvider | None = hub.resolve(profile) or hub.get("local")
        if provider is None:
            yield _sse_event("failed", "No RuntimeProvider available — check server configuration")
            return

        spec = _build_workspace_spec(profile)
        provider_ref = await provider.create_workspace(spec)
        yield _sse_event("provisioned", f"Workspace created via {type(provider).__name__}: {provider_ref}")
        await asyncio.sleep(0.3)

        # Run echo command
        yield _sse_event("command_start", 'Executing: echo "Hello from Modulo sandbox"')
        result = await provider.exec_command(provider_ref, ["echo", "Hello from Modulo sandbox"], timeout=30)
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

        # Destroy
        yield _sse_event("destroying", "Destroying sandbox...")
        await provider.destroy_workspace(provider_ref)
        yield _sse_event("destroyed", "Sandbox destroyed successfully")
    except Exception:
        _log.exception("Sandbox test failed for profile %s", profile.id)
        yield _sse_event("failed", "Test failed — check server logs for details")
        if provider_ref and provider is not None:
            try:
                await provider.destroy_workspace(provider_ref)
            except Exception:
                _log.warning("Failed to clean up sandbox %s after error", provider_ref)


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

    return WorkspaceSpec(
        environment_profile_id=profile.id,
        organisation_id=profile.organisation_id,
        run_id=None,
        image_ref=profile.image_ref,
        capabilities=profile.capabilities,
        timeout_seconds=profile.timeout_seconds,
        resource_limits=profile.resource_limits_json,
        egress_policy=profile.egress_policy or "deny_all",
        persistence_policy=profile.persistence_policy,
        labels={"profile_name": profile.name},
    )


@router.post("/{profile_id}/test")
async def test_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> StreamingResponse:
    """Provision a sandbox from the profile, run echo, destroy it — stream events."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
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
