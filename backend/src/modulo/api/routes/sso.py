"""SSO routes: OIDC and SAML 2.0 login flows."""

import logging
import xml.sax.saxutils
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_DB_ERROR_PLEASE_TRY, MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR_NO_PERIOD
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    get_db_session,
    get_system_db_session,
    require_feature_anonymous,
    resolve_anonymous_plan_context,
)
from modulo.auth.sso import (
    _set_default_rls_org,
    oidc_get_authorize_url,
    oidc_process_callback,
    parse_oidc_providers,
    saml_get_auth_url,
    saml_process_response,
)
from modulo.core.sanitize_log import sanitise_log_value
from modulo.db.crud.sso_provider import get_enabled_saml_provider, list_enabled_oidc_providers
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["sso"])


def _frontend_url(settings: Settings) -> str:
    """Derive the frontend base URL (delegates to the shared resolver)."""
    from modulo.api.frontend_url import resolve_frontend_url

    return resolve_frontend_url(settings)


def _redirect_to_frontend(tokens: dict[str, str], settings: Settings) -> RedirectResponse:
    """Redirect the browser to the frontend callback URL with tokens in fragment."""
    base = _frontend_url(settings)
    url = f"{base}/auth/callback#access_token={tokens['access_token']}&refresh_token={tokens['refresh_token']}"
    return RedirectResponse(url=url)


class OidcProviderInfo(BaseModel):
    provider_id: str
    display_name: str = ""
    preset: str = "custom"


class SsoProvidersResponse(BaseModel):
    oidc: list[OidcProviderInfo]
    saml: bool


async def _list_enabled_oidc_global(
    system_session: AsyncSession | None,
    app_session: AsyncSession,
) -> list[Any]:
    """List enabled OIDC providers via the system session (global), then app fallback.

    The system session (``modulo_system`` role, BYPASSRLS) sees every org's
    providers; when it returns nothing (role unprovisioned -> zero rows), fall
    back to the app session scoped to the first org (single-org behaviour).
    """
    providers: list[Any] = []
    if system_session is not None:
        async with system_session.begin():
            providers = await list_enabled_oidc_providers(system_session)
    if not providers:
        await _set_default_rls_org(app_session)
        providers = await list_enabled_oidc_providers(app_session)
    return providers


async def _get_enabled_saml_global(
    system_session: AsyncSession | None,
    app_session: AsyncSession,
) -> Any:
    """Return an enabled SAML provider via the system session (global), then app fallback."""
    provider: Any = None
    if system_session is not None:
        async with system_session.begin():
            provider = await get_enabled_saml_provider(system_session)
    if provider is None:
        await _set_default_rls_org(app_session)
        provider = await get_enabled_saml_provider(app_session)
    return provider


async def is_saml_available(
    settings: Settings,
    system_session: AsyncSession | None,
    app_session: AsyncSession,
) -> bool:
    """Return whether SAML sign-in is available on this instance.

    SAML is available when either:
    1. An enabled SAML provider exists in the DB with metadata configured, OR
    2. The env-var SAML config is fully set (enabled + license + metadata URL/XML).

    NOTE: SAML is single-provider-per-instance (``get_enabled_saml_provider``
    returns the first enabled provider globally via ``.limit(1)``).  This is
    therefore an instance-wide boolean — there is no per-org SAML support yet.
    """
    db_saml = await _get_enabled_saml_global(system_session, app_session)
    db_saml_ok = db_saml is not None and bool(db_saml.metadata_xml or db_saml.metadata_url)
    return db_saml_ok or (
        settings.modulo_saml_enabled
        and bool(settings.modulo_license_key)
        and (bool(settings.modulo_saml_idp_metadata_url) or bool(settings.modulo_saml_idp_metadata_xml))
    )


async def _anonymous_plan_context(settings: Settings, session: AsyncSession) -> Any:
    """Resolve the plan context WITHOUT a user (pre-auth login-page surface).

    DEPRECATED: prefer ``resolve_anonymous_plan_context`` from
    ``modulo.api.dependencies`` — this wrapper is retained for backward
    compatibility with callers that import from this module directly.
    """
    return await resolve_anonymous_plan_context(settings, session)


@router.get("/sso/providers")
@handle_db_errors("sso.sso_providers")
async def sso_providers(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> SsoProvidersResponse:
    """List configured SSO providers (OIDC) and whether SAML is enabled.

    Pre-auth discovery endpoint: the login page fetches it BEFORE any user is
    authenticated, so it must never require a user and must never surface an
    auth error. Plan/feature resolution is anonymous (via
    ``_anonymous_plan_context``); when the SSO feature is not enabled /
    unlicensed the endpoint answers a normal 200 with an EMPTY provider list
    (no 401/402) so the login page simply renders no SSO options. The
    anonymous path resolves system-level licenses only — org-level licenses
    are unreachable pre-auth.

    OIDC providers are merged from the sso_providers DB table (preferred) and
    the env-var fallback, deduplicated by provider_id. The DB read goes through
    the system session (``modulo_system`` role, instance-global) so the login
    page reflects every org's enabled providers; when the system read returns
    nothing (unprovisioned role), fall back to the app session scoped to the
    first org (single-org self-hosted behaviour). SAML is enabled if any enabled
    SAML provider exists in the DB, or if env-var SAML is fully configured
    (enabled + license + metadata).
    """
    plan = await _anonymous_plan_context(settings, session)
    if not plan.feature_enabled("sso"):
        return SsoProvidersResponse(oidc=[], saml=False)

    try:
        async with session.begin():
            oidc_providers = await _list_enabled_oidc_global(system_session, session)
            db_ids = {p.provider_id for p in oidc_providers}
            oidc_list: list[dict[str, str]] = [
                {
                    "provider_id": p.provider_id,
                    "display_name": p.name or getattr(p, "preset", "custom"),
                    "preset": getattr(p, "preset", "custom"),
                }
                for p in oidc_providers
                if p.provider_id
            ]

            for env_provider in parse_oidc_providers(settings):
                env_id = env_provider["provider_id"]
                if env_id not in db_ids:
                    oidc_list.append(
                        {
                            "provider_id": env_id,
                            "display_name": env_id,
                            "preset": "custom",
                        }
                    )

            saml_enabled = await is_saml_available(settings, system_session, session)
            return SsoProvidersResponse(
                oidc=[OidcProviderInfo(**p) for p in oidc_list],
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
    _request: Request,
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> Any:
    """Redirect the user to the OIDC provider's authorization page."""
    public_url = settings.modulo_public_url.rstrip("/")
    redirect_uri = f"{public_url}/api/v1/auth/oidc/{provider}/callback"

    try:
        async with session.begin():
            auth_url, _ = await oidc_get_authorize_url(provider, settings, redirect_uri, system_session, session)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except HTTPException:
        raise

    return Response(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": auth_url})


@router.get("/oidc/{provider}/callback")
@handle_db_errors("sso.oidc_callback")
async def oidc_callback(
    provider: str,
    request: Request,
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
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
            tokens = await oidc_process_callback(code, state, settings, system_session, session, redirect_uri)
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
            detail=MSG_DB_ERROR_PLEASE_TRY,
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
    _request: Request,
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> Any:
    """Redirect the user to the SAML IdP for authentication."""
    public_url = settings.modulo_public_url.rstrip("/")
    acs_url = f"{public_url}/api/v1/auth/saml/acs"

    try:
        async with session.begin():
            auth_url, _ = await saml_get_auth_url(settings, acs_url, system_session, session)
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
            detail=MSG_DB_ERROR_PLEASE_TRY,
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
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
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
            tokens = await saml_process_response(raw_saml, settings, system_session, session)
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
            detail=MSG_DB_ERROR_PLEASE_TRY,
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
    _request: Request,
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> str:
    """Return SP metadata XML for SAML IdP configuration."""
    try:
        async with session.begin():
            db_saml = await _get_enabled_saml_global(system_session, session)
            # A DB-configured SAML provider is self-sufficient (no env flag needed),
            # mirroring saml_login/saml_acs which resolve the DB provider without the
            # modulo_saml_enabled flag. Only when no DB provider exists do we require
            # the env flag, preserving the pure-env-var contract.
            if db_saml is None and not settings.modulo_saml_enabled:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="SAML is not enabled",
                )
            entity_id = (db_saml.entity_id if db_saml is not None else None) or settings.modulo_saml_entity_id

            public_url = settings.modulo_public_url.rstrip("/")
            acs_url = f"{public_url}/api/v1/auth/saml/acs"

            # XML-escape before embedding (FAR-915 / GitHub #281): entity_id is
            # org-configurable (DB-backed), so a malicious value must not be
            # able to inject XML or break out of the entityID attribute.
            # escape() covers & < >; the additional map covers quotes since the
            # values are interpolated inside double-quoted attributes.
            xml_entities = {'"': "&quot;", "'": "&apos;"}
            safe_entity_id = xml.sax.saxutils.escape(entity_id, xml_entities)
            safe_acs_url = xml.sax.saxutils.escape(acs_url, xml_entities)

            return (
                '<?xml version="1.0"?>'
                "<md:EntityDescriptor"
                ' xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"'
                f' entityID="{safe_entity_id}">'
                "  <md:SPSSODescriptor"
                '   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
                f"    <md:AssertionConsumerService"
                f'     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
                f'     Location="{safe_acs_url}"'
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
