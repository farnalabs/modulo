"""SSO routes: OIDC and SAML 2.0 login flows."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR_NO_PERIOD
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature
from modulo.auth.sso import (
    list_oidc_providers,
    oidc_get_authorize_url,
    oidc_process_callback,
    parse_oidc_providers,
    resolve_oidc_provider_org,
    saml_get_auth_url,
    saml_process_response,
)
from modulo.core.sanitize_log import sanitise_log_value
from modulo.settings import Settings, get_settings

_MSG_DATABASE_ERROR_PLEASE_TRY = "Database error. Please try again."


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["sso"])


def _frontend_url(settings: Settings) -> str:
    """Derive the frontend base URL from CORS_ORIGINS (first origin)."""
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    return origins[0] if origins else "http://localhost:5173"


def _redirect_to_frontend(tokens: dict[str, str], settings: Settings) -> RedirectResponse:
    """Redirect the browser to the frontend callback URL with tokens in fragment."""
    base = _frontend_url(settings)
    url = f"{base}/auth/callback#access_token={tokens['access_token']}&refresh_token={tokens['refresh_token']}"
    return RedirectResponse(url=url)


class OidcProviderInfo(BaseModel):
    provider_id: str


class SsoProvidersResponse(BaseModel):
    oidc: list[OidcProviderInfo]
    saml: bool


@router.get("/sso/providers")
@handle_db_errors("sso.sso_providers")
async def sso_providers(
    _: object = require_feature("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> SsoProvidersResponse:
    """List configured SSO providers (OIDC) and whether SAML is enabled.

    OIDC providers come from the DB ``sso_providers`` table (admin UI) first,
    falling back to ``MODULO_OIDC_PROVIDERS`` for env-var-only deployments.
    """
    try:
        async with session.begin():
            oidc_providers = await list_oidc_providers(session, org_id=None, fernet_key=settings.fernet_key)
        if not oidc_providers:
            oidc_providers = [{"provider_id": p["provider_id"]} for p in parse_oidc_providers(settings)]
        saml_enabled = (
            settings.modulo_saml_enabled
            and bool(settings.modulo_license_key)
            and (bool(settings.modulo_saml_idp_metadata_url) or bool(settings.modulo_saml_idp_metadata_xml))
        )
        return SsoProvidersResponse(
            oidc=[OidcProviderInfo(provider_id=p["provider_id"]) for p in oidc_providers],
            saml=saml_enabled,
        )
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("sso.sso_providers.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------


@router.get("/oidc/{provider}/login")
@handle_db_errors("sso.oidc_login")
async def oidc_login(
    provider: str,
    request: Request,
    _: object = require_feature("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Redirect the user to the OIDC provider's authorization page.

    Providers configured via the admin UI (the DB ``sso_providers`` table) are
    resolved first; providers from ``MODULO_OIDC_PROVIDERS`` remain supported
    as a fallback for env-var-only deployments. This is a pre-auth route, so
    the DB lookup is org-agnostic.
    """
    public_url = settings.modulo_public_url.rstrip("/")
    redirect_uri = f"{public_url}/api/v1/auth/oidc/{provider}/callback"

    try:
        async with session.begin():
            auth_url, _ = await oidc_get_authorize_url(provider, settings, redirect_uri, session=session, org_id=None)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("sso.oidc_login.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return Response(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": auth_url})


@router.get("/oidc/{provider}/callback")
@handle_db_errors("sso.oidc_callback")
async def oidc_callback(
    provider: str,
    request: Request,
    _: object = require_feature("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Handle the OIDC provider's callback (authorization code exchange).

    On success, redirects the browser to the frontend callback URL with
    access and refresh tokens as query parameters.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'code' or 'state' query parameter",
        )

    public_url = settings.modulo_public_url.rstrip("/")
    redirect_uri = f"{public_url}/api/v1/auth/oidc/{provider}/callback"

    try:
        async with session.begin():
            org_id = await resolve_oidc_provider_org(session, provider)
            tokens = await oidc_process_callback(code, state, settings, session, redirect_uri, org_id=org_id)
    except ValueError as exc:
        _log.warning(
            "OIDC callback failed for provider %s: %s",
            sanitise_log_value(provider),
            sanitise_log_value(exc),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from None
    except ProgrammingError as exc:
        _log.warning("OIDC callback failed — DB table missing: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("OIDC callback DB error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("sso.oidc_callback.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return _redirect_to_frontend(tokens, settings)


# ---------------------------------------------------------------------------
# SAML 2.0
# ---------------------------------------------------------------------------


@router.get("/saml/login")
@handle_db_errors("sso.saml_login")
async def saml_login(
    request: Request,
    _: object = require_feature("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> Any:
    """Redirect the user to the SAML IdP for authentication."""
    public_url = settings.modulo_public_url.rstrip("/")
    acs_url = f"{public_url}/api/v1/auth/saml/acs"

    try:
        auth_url, _ = await saml_get_auth_url(settings, acs_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.warning("SAML login failed — DB table missing: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SAML login DB error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("sso.saml_login.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return Response(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": auth_url})


@router.post("/saml/acs")
@handle_db_errors("sso.saml_acs")
async def saml_acs(
    request: Request,
    _: object = require_feature("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Handle the SAML Assertion Consumer Service POST from the IdP.

    On success, redirects the browser to the frontend callback URL with
    access and refresh tokens as query parameters.
    """
    form = await request.form()
    raw_saml: object = form.get("SAMLResponse", "")
    if not isinstance(raw_saml, str) or not raw_saml:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'SAMLResponse' in form data",
        )

    try:
        async with session.begin():
            tokens = await saml_process_response(raw_saml, settings, session)
    except ValueError as exc:
        _log.warning("SAML ACS failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from None
    except ProgrammingError as exc:
        _log.warning("SAML ACS failed — DB table missing: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("SAML ACS DB error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("sso.saml_acs.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return _redirect_to_frontend(tokens, settings)


@router.get("/saml/metadata", response_class=PlainTextResponse)
@handle_db_errors("sso.saml_metadata")
async def saml_metadata(
    request: Request,
    _: object = require_feature("sso"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Return SP metadata XML for SAML IdP configuration."""
    if not settings.modulo_saml_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML is not enabled",
        )

    try:
        public_url = settings.modulo_public_url.rstrip("/")
        acs_url = f"{public_url}/api/v1/auth/saml/acs"
        entity_id = settings.modulo_saml_entity_id

        return (
            '<?xml version="1.0"?>'
            "<md:EntityDescriptor"
            ' xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"'
            f' entityID="{entity_id}">'
            "  <md:SPSSODescriptor"
            '   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            f"    <md:AssertionConsumerService"
            f'     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
            f'     Location="{acs_url}"'
            f'     index="1"/>'
            "  </md:SPSSODescriptor>"
            "</md:EntityDescriptor>"
        )
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("sso.saml_metadata.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e
