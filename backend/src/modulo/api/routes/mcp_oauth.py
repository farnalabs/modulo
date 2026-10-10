"""OAuth 2.0 client management endpoints (browser-authenticated) + the
anonymous consent-context read (FAR-1476).

POST /api/v1/mcp/oauth/clients              — Register a new OAuth client
GET  /api/v1/mcp/oauth/clients               — List OAuth clients
DELETE /api/v1/mcp/oauth/clients/{id}        — Delete an OAuth client
POST /api/v1/mcp/oauth/consent/approve       — Approve a pending browser consent
                                                (optionally narrowing the grant)
GET  /api/v1/mcp/oauth/consent/context       — Anonymous display context for a
                                                pending consent (FAR-1476 slice 3)

Every AUTHENTICATED route here carries ``require_feature("mcp_server")``
(FAR-1283): the flag is the operator kill switch for the whole MCP capability,
so the backend has to honour it — the settings-page ``FeatureGate`` only hides
the UI.

The protocol endpoints (GET /mcp/oauth/authorize, POST /mcp/oauth/token,
POST /mcp/oauth/refresh) live in the MCP sub-app at ``mcp_server.py``, and the
consent-context read above lives here — all three are reachable WITHOUT a
session, so they must NOT use ``require_feature`` (whose plan resolution
depends on the authenticated ``get_current_user`` chain and would 401 the
pre-auth caller) — ``mcp_server`` is enforced there against the pending
consent row's resolved org instead (the ``_oauth_authorize`` pattern).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_DB_OPERATION_FAILED,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_UNEXPECTED_ERROR_NO_PERIOD,
)
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_feature
from modulo.api.team_scope import team_membership_exists
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.oauth import (
    InvalidGrantError,
    InvalidRedirectUriError,
    InvalidScopeError,
    create_authorization_code,
    create_oauth_client,
    delete_oauth_client,
    list_oauth_clients,
    normalize_redirect_uris,
    normalize_scopes,
    validate_redirect_uri,
    verify_live_role_covers_scopes,
)
from modulo.core.audit_coverage import audited
from modulo.core.runtime_config.key_bridge import public_url_is_configured
from modulo.db.models.oauth_client import OAuthClient
from modulo.db.models.oauth_token import OAuthConsentState
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp/oauth", tags=["mcp-oauth"])


class CreateOAuthClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    redirect_uris: list[str] = Field(min_length=1, description="Allowed redirect URIs")
    scopes: list[str] = Field(min_length=1, description="Allowed scopes")
    # FAR-1476: optional team boundary. NULL = an org-wide client (admin/operator
    # only); a runner-registered client MUST bind one of its member teams.
    team_id: uuid.UUID | None = None


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


async def _validate_oauth_team_binding(
    session: AsyncSession,
    principal: TenantPrincipal,
    team_id: uuid.UUID | None,
) -> None:
    """Validate the optional ``team_id`` on OAuth client registration (FAR-1476).

    Membership rule (explicit, tested):
    - ``admin``/``operator`` may bind any team in their org, or none (org-wide).
    - ``runner`` may bind ONLY a team they are a member of, and MUST bind one —
      a runner-registered client is never org-wide (no NULL boundary).

    The team must exist (not soft-deleted) in the caller's organisation, else
    404 — a foreign-org or non-existent team is never silently persisted (the
    same-org tenant trigger would also reject it at the DB layer). Runs inside
    the RLS-scoped transaction, so the org filter is enforced by the RLS policy
    as well as the explicit ``organisation_id`` predicate.
    """
    if team_id is None:
        if principal.org_role == "runner":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Runner-registered OAuth clients must be bound to a team the runner is a member of",
            )
        return
    result = await session.execute(
        select(Team.id).where(
            Team.id == team_id,
            Team.organisation_id == principal.organisation_id,
            Team.deleted_at.is_(None),
        )
    )
    if result.first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_id} not found in this organisation.",
        )
    if principal.org_role == "runner":
        is_member = await team_membership_exists(session, account_id=principal.account_id, team_id=team_id)
        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Runner can only bind an OAuth client to a team they are a member of",
            )


# Minting an OAuth client secret is a credential grant: an unaudited mint would
# be unattributable -> fail closed.
@router.post(
    "/clients",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(deny_break_glass_mint),
        require_feature("mcp_server"),
        Depends(
            audited("oauth_client_created", "oauth_client", principal_dep=get_current_tenant_user, fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        ),
    ],
)
@handle_db_errors("mcp_oauth.register_oauth_client")
async def register_oauth_client(
    req: CreateOAuthClientRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> CreateOAuthClientResponse:
    # FAR-1476 (D1): runners may register a team-bound OAuth client. The
    # per-role team-binding rule (runner must bind a member team; admin/operator
    # may bind any org team or none) is enforced by
    # ``_validate_oauth_team_binding`` below, inside the RLS transaction.
    if principal.org_role not in ("admin", "operator", "runner"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin, operator or runner users can register OAuth clients",
        )

    if not public_url_is_configured(settings):
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

    # FAR-1281: validate BEFORE the join so what is stored is exactly what was
    # validated (an entry with an internal space would round-trip as two URIs
    # through the space-joined column) and so a forbidden URI is never stored.
    try:
        redirect_uris = normalize_redirect_uris(req.redirect_uris)
    except InvalidRedirectUriError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    redirect_uris_str = " ".join(redirect_uris)
    scopes_str = " ".join(req.scopes)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await _validate_oauth_team_binding(session, principal, req.team_id)
            client, raw_secret = await create_oauth_client(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                scopes=scopes_str,
                redirect_uris=redirect_uris_str,
                created_by=principal.account_id,
                team_id=req.team_id,
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_oauth.register_oauth_client")
        _log.exception("mcp_oauth.register_oauth_client")
        _log.warning(
            "mcp_oauth.register_oauth_client.sqlalchemy_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
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


@router.get("/clients", dependencies=[require_feature("mcp_server")])
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_oauth.list_oauth_clients_endpoint")
        _log.exception("mcp_oauth.list_oauth_clients_endpoint")
        _log.warning("mcp_oauth.list_oauth_clients.sqlalchemy_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        _log.exception(
            "mcp_oauth.list_oauth_clients.unexpected_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e
    return [OAuthClientItem(**c) for c in clients]


# Revoking a client invalidates its issued credentials -> fail closed.
@router.delete(
    "/clients/{client_id}",
    dependencies=[
        Depends(deny_break_glass_mint),
        require_feature("mcp_server"),
        Depends(
            audited("oauth_client_deleted", "oauth_client", principal_dep=get_current_tenant_user, fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        ),
    ],
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_oauth.remove_oauth_client")
        _log.exception("mcp_oauth.remove_oauth_client")
        _log.warning(
            "mcp_oauth.remove_oauth_client.sqlalchemy_error",
            extra={"client_id": client_id, "org_id": str(principal.organisation_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
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
    # FAR-1476 slice 3: optional per-scope deny. ``None`` (omitted) grants every
    # stored scope — the pre-slice behaviour, unchanged. When provided it must
    # be a canonical SUBSET of the stored state row's scopes; anything outside
    # that set is rejected (fail closed — the code can never carry more than the
    # authorize leg stored).
    granted_scopes: list[str] | None = None


class ConsentApproveResponse(BaseModel):
    redirect_url: str


# Consent is the grant that lets an OAuth client act for this user -> fail closed.
@router.post(
    "/consent/approve",
    dependencies=[
        require_feature("mcp_server"),
        Depends(
            audited(
                "oauth_consent_approved",
                "oauth_consent",
                principal_dep=get_current_tenant_user,
                fail_closed=True,
            ),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        ),
    ],
)
@handle_db_errors("mcp_oauth.approve_consent")
async def approve_consent(
    req: ConsentApproveRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ConsentApproveResponse:
    """Approve a pending OAuth consent (ADR 047 DECISION 1 — the approve POST IS the consent).

    The authenticated approve POST is the human approval: the Bearer principal
    IS the consenting account. The browser consent page (FAR-1476 slice 3)
    renders the pending grant set from ``GET /consent/context`` and lets the
    human deny individual scopes; ``state`` is a client-chosen
    correlation/replay-binding nonce — the Bearer requirement is the
    consent-CSRF control (a cross-origin auto-POST cannot attach a
    localStorage Bearer).

    Security properties:
    - ``state`` must be single-use, unexpired, and in the approver's org (RLS).
    - ``redirect_uri`` comes from the state row ONLY — never client-supplied,
      and it is re-validated before it is used to build the redirect.
    - The code is minted from the state row's scopes + code_challenge ONLY, so
      a tampered display can never escalate the granted scope (display is
      never authoritative). ``granted_scopes`` can only NARROW that set: it is
      canonicalised, must be a non-empty subset of the stored scopes (anything
      outside fails closed with 400, never widens), and the live-role check
      re-runs against the granted subset so a demotion still degrades.
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

            # FAR-1281: fail closed if the stored redirect_uri would not pass
            # today's registration rules. The state row can predate them (the
            # TTL is ~15 min), and this handler is what hands the browser the
            # final redirect target.
            try:
                validate_redirect_uri(state_row.redirect_uri)
            except InvalidRedirectUriError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                ) from e

            # FAR-1476 slice 3: resolve the granted set. Omitted = every stored
            # scope (pre-slice behaviour). Provided = canonical subset only.
            stored_scopes = list(state_row.scopes)
            if req.granted_scopes is None:
                granted_scopes = stored_scopes
            else:
                try:
                    granted_scopes = sorted(set(normalize_scopes(" ".join(req.granted_scopes))))
                except InvalidScopeError as e:
                    # Unknown scope keys fail closed before the subset check —
                    # an unrecognised key can never ride along.
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=str(e),
                    ) from e
                if not granted_scopes:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="granted_scopes must grant at least one scope; decline the consent instead",
                    )
                outside = sorted(set(granted_scopes) - set(stored_scopes))
                if outside:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="granted_scopes must be a subset of the requested scopes; not requested: "
                        + ", ".join(outside),
                    )
                # Re-verify the live role against the GRANTED subset (the token
                # endpoint re-checks again at exchange; a demotion between
                # authorize and approve degrades here too, fail closed).
                try:
                    await verify_live_role_covers_scopes(
                        session,
                        account_id=principal.account_id,
                        org_id=state_row.organisation_id,
                        scopes=granted_scopes,
                    )
                except InvalidGrantError as e:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=str(e),
                    ) from e

            code = await create_authorization_code(
                session,
                client_id=state_row.client_id,
                org_id=state_row.organisation_id,
                scopes=" ".join(granted_scopes),
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_oauth.approve_consent")
        _log.exception("mcp_oauth.approve_consent")
        _log.warning("mcp_oauth.approve_consent.sqlalchemy_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("mcp_oauth.approve_consent.unexpected_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    # The registered URI may already carry a query, so join with "&" in that
    # case — "?code=" on top of "?x=1" would strand the code in the "x" value.
    separator = "&" if "?" in state_row.redirect_uri else "?"
    redirect_url = f"{state_row.redirect_uri}{separator}code={quote(code)}&state={quote(req.state)}"
    return ConsentApproveResponse(redirect_url=redirect_url)


# ---------------------------------------------------------------------------
# Consent context — anonymous, pre-auth (FAR-1476 slice 3)
# ---------------------------------------------------------------------------


class ConsentContextTeam(BaseModel):
    id: str
    name: str


class ConsentContextResponse(BaseModel):
    """Display-only context for the pending consent (FAR-1476 slice 3).

    Nothing here is authoritative: the approve POST mints the code from the
    stored state row only, so a tampered display can never escalate the grant.
    Deliberately carries NO redirect_uri, no secrets and no org internals.
    """

    client_name: str
    scopes: list[str]
    team: ConsentContextTeam | None = None


async def _query_pending_consent_state(session: AsyncSession, state: str) -> OAuthConsentState | None:
    """Read an unexpired, unconsumed consent state WITHOUT consuming it.

    Deliberately a plain SELECT (no ``consume`` side effect) so the browser can
    render the grant set before the human decides. RLS note: this read runs
    BEFORE any org context is known (the org is a column OF this row), which is
    why migration 0295 widened the table's policy with the NULL-context arm —
    the same contract ``oauth_clients`` already has for the pre-auth authorize
    read.
    """
    result = await session.execute(
        select(OAuthConsentState).where(
            OAuthConsentState.state == state,
            OAuthConsentState.consumed.is_(False),
            OAuthConsentState.expires_at > datetime.now(UTC),
        )
    )
    return result.scalar_one_or_none()


async def _query_oauth_client_for_consent(session: AsyncSession, client_id: str) -> OAuthClient | None:
    """Read the consent's OAuth client (display name + team boundary)."""
    result = await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    return result.scalar_one_or_none()


async def _query_team_for_consent(session: AsyncSession, team_id: uuid.UUID) -> Team | None:
    """Read the client's team row for the team-boundary display line."""
    result = await session.execute(select(Team).where(Team.id == team_id))
    return result.scalar_one_or_none()


@router.get("/consent/context", response_model=None)
@handle_db_errors("mcp_oauth.consent_context")
async def consent_context(
    state: str = Query(min_length=1, max_length=128),
    session: AsyncSession = Depends(get_db_session),
) -> ConsentContextResponse | JSONResponse:
    """GET /api/v1/mcp/oauth/consent/context?state=... — display context for the
    browser consent page (FAR-1476 slice 3).

    ANONYMOUS / pre-auth by contract: the caller is the not-yet-signed-in
    browser that just landed on the SPA consent route from the authorize 302,
    so this route must NOT use ``require_feature`` / ``get_current_tenant_user``
    (their plan resolution composes the authenticated ``get_current_user``
    chain and would 401 before the handler ran). The org is resolved from the
    pending consent row itself and ``mcp_server`` is enforced against it —
    the ``_oauth_authorize`` pattern.

    Returns the canonical scope keys stored by the authorize leg (already
    canonicalised + ceiling-intersected), the client's display name, and its
    team boundary when set. Unknown / expired / consumed states are a single
    404 that does not leak whether a client exists. Nothing beyond the display
    context is returned — never the redirect_uri, secrets, or org internals.
    """
    # Imported here (not at module level) to avoid a circular import with the
    # MCP sub-app, which imports route modules during its own construction.
    from modulo.api.mcp_server import _mcp_feature_unavailable_response, _mcp_server_flag_enabled

    try:
        async with session.begin():
            state_row = await _query_pending_consent_state(session, state)
            if state_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Consent request not found",
                )

            # Bind the session's org BEFORE the client/team reads and the flag
            # resolution, so every subsequent read is org-scoped (teams carries
            # a team-isolation policy that needs the org GUC).
            await set_rls_org(session, state_row.organisation_id)

            # FAR-1283, pre-auth edition: the kill switch is enforced against
            # the org the caller just proved (the pending consent row) without
            # an authenticated dependency chain.
            if not await _mcp_server_flag_enabled(state_row.organisation_id, session):
                return _mcp_feature_unavailable_response()

            client_row = await _query_oauth_client_for_consent(session, state_row.client_id)
            if client_row is None:
                # Client deleted mid-flow — same undifferentiated 404.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Consent request not found",
                )

            team: ConsentContextTeam | None = None
            if client_row.team_id is not None:
                team_row = await _query_team_for_consent(session, client_row.team_id)
                if team_row is not None:
                    team = ConsentContextTeam(id=str(team_row.id), name=team_row.name)

            return ConsentContextResponse(
                client_name=client_row.name,
                scopes=list(state_row.scopes),
                team=team,
            )
    except ProgrammingError:
        _log.exception("mcp_oauth.consent_context")
        _log.warning("mcp_oauth.consent_context.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_oauth.consent_context")
        _log.exception("mcp_oauth.consent_context")
        _log.warning("mcp_oauth.consent_context.sqlalchemy_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("mcp_oauth.consent_context.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e
