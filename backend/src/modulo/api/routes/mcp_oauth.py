"""OAuth 2.0 client management endpoints (browser-authenticated).

POST /api/v1/mcp/oauth/clients         — Register a new OAuth client
GET  /api/v1/mcp/oauth/clients          — List OAuth clients
DELETE /api/v1/mcp/oauth/clients/{id}   — Delete an OAuth client
POST /api/v1/mcp/oauth/consent/approve  — Approve a pending browser consent

The protocol endpoints (GET /mcp/oauth/authorize, POST /mcp/oauth/token,
POST /mcp/oauth/refresh) live in the MCP sub-app at ``mcp_server.py``.
"""

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR_NO_PERIOD
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.oauth import (
    InvalidScopeError,
    create_authorization_code,
    create_oauth_client,
    delete_oauth_client,
    list_oauth_clients,
    normalize_scopes,
)
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_MSG_DATABASE_ERROR_OCCURRED_PLEASE = "Database error occurred. Please try again."


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp/oauth", tags=["mcp-oauth"])


class CreateOAuthClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    redirect_uris: list[str] = Field(min_length=1, description="Allowed redirect URIs")
    scopes: list[str] = Field(min_length=1, description="Allowed scopes")


class CreateOAuthClientResponse(BaseModel):
    id: str
    client_id: str
    client_secret: str
    name: str


class OAuthClientItem(BaseModel):
    id: str
    client_id: str
    name: str
    scopes: list[str]
    redirect_uris: list[str]
    created_at: str


class DeleteOAuthClientResponse(BaseModel):
    deleted: bool


@router.post(
    "/clients",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("mcp_oauth.register_oauth_client")
async def register_oauth_client(
    req: CreateOAuthClientRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> CreateOAuthClientResponse:
    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or operator users can register OAuth clients",
        )

    if not settings.modulo_public_url or settings.modulo_public_url == "http://localhost:8000":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="MODULO_PUBLIC_URL must be configured for OAuth flow",
        )

    try:
        normalize_scopes(" ".join(req.scopes))
    except InvalidScopeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    redirect_uris_str = " ".join(req.redirect_uris)
    scopes_str = " ".join(req.scopes)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            client, raw_secret = await create_oauth_client(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                scopes=scopes_str,
                redirect_uris=redirect_uris_str,
                created_by=principal.account_id,
            )
    except ProgrammingError:
        _log.exception("mcp_oauth.register_oauth_client")
        _log.warning(
            "mcp_oauth.register_oauth_client.programming_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("mcp_oauth.register_oauth_client")
        _log.warning(
            "mcp_oauth.register_oauth_client.sqlalchemy_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception as e:
        _log.exception(
            "mcp_oauth.register_oauth_client.unexpected_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return CreateOAuthClientResponse(
        id=str(client.id),
        client_id=client.client_id,
        client_secret=raw_secret,
        name=client.name,
    )


@router.get("/clients")
@handle_db_errors("mcp_oauth.list_oauth_clients_endpoint")
async def list_oauth_clients_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[OAuthClientItem]:
    # SECURITY (#1307): match the create/delete role gate — viewers should not
    # enumerate OAuth clients (exposes redirect_uris, scopes attack surface).
    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or operator users can list OAuth clients",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            clients = await list_oauth_clients(session, principal.organisation_id)
    except ProgrammingError:
        _log.exception("mcp_oauth.list_oauth_clients_endpoint")
        _log.warning("mcp_oauth.list_oauth_clients.programming_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("mcp_oauth.list_oauth_clients_endpoint")
        _log.warning("mcp_oauth.list_oauth_clients.sqlalchemy_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception as e:
        _log.exception(
            "mcp_oauth.list_oauth_clients.unexpected_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e
    return [OAuthClientItem(**c) for c in clients]


@router.delete(
    "/clients/{client_id}",
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("mcp_oauth.remove_oauth_client")
async def remove_oauth_client(
    client_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> DeleteOAuthClientResponse:
    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or operator users can delete OAuth clients",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await delete_oauth_client(session, client_id=client_id, org_id=principal.organisation_id)
    except ProgrammingError:
        _log.exception("mcp_oauth.remove_oauth_client")
        _log.warning(
            "mcp_oauth.remove_oauth_client.programming_error",
            extra={"client_id": client_id, "org_id": str(principal.organisation_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("mcp_oauth.remove_oauth_client")
        _log.warning(
            "mcp_oauth.remove_oauth_client.sqlalchemy_error",
            extra={"client_id": client_id, "org_id": str(principal.organisation_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception as e:
        _log.exception(
            "mcp_oauth.remove_oauth_client.unexpected_error",
            extra={"client_id": client_id, "org_id": str(principal.organisation_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="OAuth client not found",
        )
    return DeleteOAuthClientResponse(deleted=True)


# ---------------------------------------------------------------------------
# Consent approve — the ONLY authenticated endpoint in the OAuth flow
# ---------------------------------------------------------------------------


class ConsentApproveRequest(BaseModel):
    state: str = Field(min_length=1, max_length=128)


class ConsentApproveResponse(BaseModel):
    redirect_url: str


@router.post("/consent/approve")
@handle_db_errors("mcp_oauth.approve_consent")
async def approve_consent(
    req: ConsentApproveRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ConsentApproveResponse:
    """Approve a pending OAuth consent (ADR 017 DECISION 1 — the approve POST IS the consent).

    The authenticated approve POST is the human approval: the Bearer principal
    IS the consenting account. There is deliberately NO consent page / deny
    affordance (deferred until an interactive customer exists). ``state`` is a
    client-chosen correlation/replay-binding nonce — the Bearer requirement is
    the consent-CSRF control (a cross-origin auto-POST cannot attach a
    localStorage Bearer).

    Security properties:
    - ``state`` must be single-use, unexpired, and in the approver's org (RLS).
    - ``redirect_uri`` comes from the state row ONLY — never client-supplied.
    - The code is minted from the state row's scopes + code_challenge ONLY, so
      a tampered display can never escalate the granted scope (display is
      never authoritative).
    - The returned ``redirect_url`` is server-derived: ``redirect_uri?code=..&state=..``.
    """
    from modulo.auth.oauth import consume_consent_state

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            state_row = await consume_consent_state(
                session,
                state=req.state,
                _org_id=principal.organisation_id,
                account_id=principal.account_id,
            )
            if state_row is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Unknown, expired, or already-used consent state",
                )

            code = await create_authorization_code(
                session,
                client_id=state_row.client_id,
                org_id=state_row.organisation_id,
                scopes=" ".join(state_row.scopes),
                redirect_uri=state_row.redirect_uri,
                account_id=principal.account_id,
                code_challenge=state_row.code_challenge,
                code_challenge_method="S256",
            )
    except ProgrammingError:
        _log.exception("mcp_oauth.approve_consent")
        _log.warning("mcp_oauth.approve_consent.programming_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("mcp_oauth.approve_consent")
        _log.warning("mcp_oauth.approve_consent.sqlalchemy_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        raise exc
    except Exception as e:
        _log.exception("mcp_oauth.approve_consent.unexpected_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    redirect_url = f"{state_row.redirect_uri}?code={quote(code)}&state={quote(req.state)}"
    return ConsentApproveResponse(redirect_url=redirect_url)
