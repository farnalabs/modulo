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
from modulo.api.routes.library import LibraryPrimitiveResponse
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

_log = logging.getLogger(__name__)

_CODE_COMMUNITY_LIBRARY_INSTALL = "community_library.install"


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


async def _fetch_entry_content(content_sha256: str) -> Any:
    """Fetch and parse an entry blob, failing open to ``None`` on any error."""
    settings = get_settings()
    client = LibraryClient(
        endpoint=settings.modulo_library_endpoint,
        root_public_key_pem=settings.modulo_library_root_public_key,
        timeout_seconds=settings.modulo_library_sync_timeout_seconds,
    )
    try:
        blob = await client.fetch_blob(content_sha256)
        if blob is not None:
            return json.loads(blob.decode("utf-8"))
    except Exception:
        _log.exception("community_library.get_entry_blob")
    finally:
        await client.close()
    return None


@router.get("/{entry_id}")
async def get_entry(
    entry_id: str,
    session: AsyncSession = Depends(get_db_session),
    _principal: TenantPrincipal = require_permission("library.search"),
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
        content = await _fetch_entry_content(content_sha256)
    result = dict(entry)
    result["content"] = content
    return result


@router.post(
    "/{entry_id}/install",
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
        _log.exception(_CODE_COMMUNITY_LIBRARY_INSTALL)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from None
    except SQLAlchemyError:
        _log.exception(_CODE_COMMUNITY_LIBRARY_INSTALL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception(_CODE_COMMUNITY_LIBRARY_INSTALL)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while installing the community entry.",
        ) from None
    return LibraryPrimitiveResponse.model_validate(primitive)
