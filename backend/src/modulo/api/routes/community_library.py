"""Community library browse + install endpoints (FAR-363).

Reads the verified, cached community-library manifest via
``modulo.core.library_service.community`` and installs registry primitives
into the calling organisation. Browse endpoints are fail-open (an unavailable
or unconfigured community library yields an empty list / null detail); the
install endpoint maps the helper's ``ValueError`` messages to HTTP statuses.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_permission
from modulo.api.routes.library import (
    CommunityContributionListResponse,
    LibraryPrimitiveResponse,
    list_community_contributions_endpoint,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.core.library_service.community import (
    get_community_entry,
    install_community_entry,
    list_community_entries,
)
from modulo.core.library_sync import LibraryClient, get_cached_manifest
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings

router = APIRouter(prefix="/api/v1/libraries/community", tags=["community-library"])

# The community router is mounted before ``library_router`` so the static
# ``GET /api/v1/libraries/community`` list route is not shadowed by the
# library router's single-segment ``GET /{primitive_id}`` (UUID-typed) route,
# which would otherwise 422 on the literal ``community`` segment. Mounting it
# first means its ``GET /{entry_id}`` would shadow the library router's
# ``GET /community/contributions`` route, so that existing endpoint is
# re-exposed here under the community prefix (same handler, same contract).
router.get("/contributions", response_model=CommunityContributionListResponse)(list_community_contributions_endpoint)

_log = logging.getLogger(__name__)


class InstallRequest(BaseModel):
    target_team_id: UUID | None = None


@router.get("")
async def list_community(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("library.search"),
) -> dict[str, Any]:
    """List synced community entries, fail-open to an empty list."""
    items: list[dict[str, Any]] = []
    synced_at: str | None = None
    try:
        items = await list_community_entries(session, principal.organisation_id)
        manifest = await get_cached_manifest(session)
        if isinstance(manifest, dict):
            generated = manifest.get("generated_at")
            if isinstance(generated, str):
                synced_at = generated
    except Exception:
        _log.exception("community_library.list_community")
        items = []
        synced_at = None
    return {"items": items, "total": len(items), "synced_at": synced_at}


@router.get("/{entry_id}")
async def get_entry(
    entry_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("library.search"),
) -> dict[str, Any]:
    """Return a single community entry, including its parsed blob content."""
    try:
        entry = await get_community_entry(session, entry_id)
    except Exception:
        _log.exception("community_library.get_entry")
        entry = None
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Community entry not found",
        )
    content: Any = None
    content_sha256 = entry.get("content_sha256")
    if isinstance(content_sha256, str) and content_sha256:
        settings = get_settings()
        client = LibraryClient(
            endpoint=settings.modulo_library_endpoint,
            root_public_key_pem=settings.modulo_library_root_public_key,
            timeout_seconds=settings.modulo_library_sync_timeout_seconds,
        )
        try:
            blob = await client.fetch_blob(content_sha256)
            if blob is not None:
                content = json.loads(blob.decode("utf-8"))
        except Exception:
            _log.exception("community_library.get_entry_blob")
            content = None
        finally:
            await client.close()
    result = dict(entry)
    result["content"] = content
    return result


@router.post(
    "/{entry_id}/install",
    response_model=LibraryPrimitiveResponse,
    status_code=status.HTTP_201_CREATED,
)
async def install(
    entry_id: str,
    req: InstallRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("library.copy"),
) -> LibraryPrimitiveResponse:
    """Install a community entry into the calling organisation."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            primitive = await install_community_entry(
                session,
                principal.organisation_id,
                entry_id,
                target_team_id=req.target_team_id,
                created_by=principal.account_id,
            )
    except ValueError as exc:
        message = str(exc)
        if message == "entry not found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Community entry not found",
            ) from None
        if message == "already installed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Community entry already installed",
            ) from None
        if message == "blob fetch failed":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Community entry content could not be fetched",
            ) from None
        _log.exception("community_library.install")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from None
    except SQLAlchemyError:
        _log.exception("community_library.install")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception("community_library.install")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while installing the community entry.",
        ) from None
    return LibraryPrimitiveResponse.model_validate(primitive)
