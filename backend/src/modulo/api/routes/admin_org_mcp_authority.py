"""Org-admin control for the human-linked MCP HITL authority kill switch (FAR-1972).

ADR 063 step 1 (FAR-1957) lets a consent-derived, human-linked OAuth credential
decide ``human_only`` HITL gates over MCP, with an org-wide kill switch persisted
at ``organisations.settings_json.feature_overrides["mcp_human_linked_hitl_authority"]``.
The gate enforcement and the flag registration ship with FAR-1957; this module is
the org-admin reachability half — it lets an org admin read and set THEIR OWN
org's override without the instance-wide ``system.config.manage`` gate the
feature-flags page requires (that page, and its system-admin org-override
endpoint, keep working unchanged).

The write targets the same durable truth the feature-flags page writes (the org
``feature_overrides`` map), so both surfaces agree; the reader is the existing
``FeatureFlagRegistry.resolve_flag`` org-override layer. Until FAR-1957
registers the flag, ``resolve_flag`` has no default to consult, so the read
falls back to the documented default (ON) rather than reporting OFF.

Authz: ``org.mcp.human_linked_hitl_authority.manage`` (``admin``) via
``require_permission``, so the registry is the single source of the role.
System admins keep the feature-flags page for the instance-wide view.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from modulo.api.db_error_handling import (
    handle_db_errors,
    raise_lock_timeout_conflict_if,
    raise_session_contract_error,
)
from modulo.api.dependencies import get_db_session, require_permission, resolve_audit_principal
from modulo.api.routes._org_settings import apply_org_settings_patch_or_404
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.feature_flags import feature_overrides_from_settings, get_registry
from modulo.db.crud.organisation import get_organisation
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/org", tags=["admin", "mcp"])

# The canonical key FAR-1957 registers in ``core.feature_flags`` as a
# community-tier flag (default ON). Kept as a module constant here so the route
# never scatters the literal; once FAR-1957 lands its public constant is the
# single source and this can import it.
MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG = "mcp_human_linked_hitl_authority"

_PERMISSION_MANAGE = "org.mcp.human_linked_hitl_authority.manage"

_CODE_LIST_FAILED = "admin_org_mcp_authority.get_failed"
_CODE_SET_FAILED = "admin_org_mcp_authority.set_failed"
_MSG_ORG_NOT_FOUND = "Organisation not found."
_MSG_GET_FAILED = "Failed to read the human-linked MCP HITL authority setting."
_MSG_SET_FAILED = "Failed to update the human-linked MCP HITL authority setting."
_MSG_NOT_AVAILABLE = "This setting is not available. Run database migrations to enable it."


class McpAuthorityResponse(BaseModel):
    """The org's human-linked MCP HITL authority setting.

    ``enabled`` is the effective intent: the org override when one is set, else
    the flag's documented default. ``overridden`` reports whether the org has an
    explicit override (so the UI can distinguish "on by default" from "on
    because someone set it").
    """

    enabled: bool
    overridden: bool


class McpAuthorityUpdateRequest(BaseModel):
    enabled: bool


def _default_enabled() -> bool:
    """The flag's default, read from the registry when it is registered.

    FAR-1957 registers ``mcp_human_linked_hitl_authority`` (default ON). Before
    that registration lands the registry has no entry, and ``resolve_flag``
    would report OFF for an org with no override — the inverse of the documented
    default. Fall back to the documented default (ON) rather than misreport it;
    once registered, the registry is authoritative.
    """
    flag = get_registry().get_flag(MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG)
    if flag is None:
        return True
    return bool(flag.currently_active)


@router.get("/mcp-human-linked-hitl-authority", response_model=McpAuthorityResponse)
@handle_db_errors(_CODE_LIST_FAILED)
async def get_org_mcp_authority(
    current_user: Annotated[TenantPrincipal, require_permission(_PERMISSION_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> McpAuthorityResponse | Response:
    """Read the caller's OWN org human-linked MCP HITL authority setting."""
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORG_NOT_FOUND)
        overrides = feature_overrides_from_settings(getattr(org, "settings_json", None))
        overridden = MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG in overrides
        enabled = overrides[MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG] if overridden else _default_enabled()
        return McpAuthorityResponse(enabled=enabled, overridden=overridden)
    except HTTPException:
        raise
    except ProgrammingError:
        _log.exception(_CODE_LIST_FAILED)
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={"error": {"code": "NOT_IMPLEMENTED", "message": _MSG_NOT_AVAILABLE}},
        )
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_LIST_FAILED)
        _log.exception(_CODE_LIST_FAILED)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"code": "SERVICE_UNAVAILABLE", "message": "Database error while reading the setting."}},
        )
    except Exception:
        _log.exception(_CODE_LIST_FAILED)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "INTERNAL_ERROR", "message": _MSG_GET_FAILED}},
        )


@router.put(
    "/mcp-human-linked-hitl-authority",
    dependencies=[Depends(audited("api_access_put", "org", principal_dep=resolve_audit_principal))],
    response_model=McpAuthorityResponse,
)
@handle_db_errors(_CODE_SET_FAILED)
async def set_org_mcp_authority(
    req: McpAuthorityUpdateRequest,
    current_user: Annotated[TenantPrincipal, require_permission(_PERMISSION_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> McpAuthorityResponse | Response:
    """Set the caller's OWN org override for the human-linked MCP HITL authority.

    Writes ``feature_overrides[mcp_human_linked_hitl_authority]`` through the
    atomic nested patch (a single statement merging just this sub-field), so a
    concurrent writer of a DIFFERENT flag override is not dropped. ``enabled``
    False is the tightening direction: it refuses every MCP credential on a
    ``human_only`` gate, restoring the pre-ADR-063 behaviour.
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await apply_org_settings_patch_or_404(
                session,
                current_user.organisation_id,
                nested_patch={"feature_overrides": {MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG: req.enabled}},
            )
        return McpAuthorityResponse(enabled=req.enabled, overridden=True)
    except HTTPException:
        raise
    except ProgrammingError:
        _log.exception(_CODE_SET_FAILED)
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={"error": {"code": "NOT_IMPLEMENTED", "message": _MSG_NOT_AVAILABLE}},
        )
    except SQLAlchemyError as exc:
        # FAR-1832 QA F2: the session-contract guard leads (it returns for other
        # errors), then the lock-timeout 409 classification, before the 503.
        raise_session_contract_error(exc, _CODE_SET_FAILED)
        raise_lock_timeout_conflict_if(exc, _CODE_SET_FAILED)
        _log.exception(_CODE_SET_FAILED)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"code": "SERVICE_UNAVAILABLE", "message": "Database error while updating the setting."}},
        )
    except Exception:
        _log.exception(_CODE_SET_FAILED)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "INTERNAL_ERROR", "message": _MSG_SET_FAILED}},
        )
