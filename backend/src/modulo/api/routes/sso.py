"""SSO routes: OIDC and SAML 2.0 login flows."""

import hmac
import logging
import secrets
import uuid
import xml.sax.saxutils
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from modulo.api.constants import MSG_DB_ERROR_PLEASE_TRY, MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR_NO_PERIOD
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import (
    get_db_session,
    get_system_db_session,
    require_feature_anonymous,
    resolve_anonymous_plan_context,
)
from modulo.auth.sso import (
    SSO_ERROR_AUTH_FAILED,
    SSO_ERROR_NOT_INVITED,
    SSO_ERROR_PROVIDER_UNAVAILABLE,
    SsoCallbackError,
    SsoProvisioningDeniedError,
    _read_system_saml_provider_by_slug,
    _set_default_rls_org,
    oidc_get_authorize_url,
    oidc_process_callback,
    parse_oidc_providers,
    saml_get_auth_url,
    saml_process_response,
    sign_saml_relay_state,
    sign_state,
    verify_saml_relay_state,
    verify_state,
)
from modulo.core.audit_coverage import audited_system, bind_audit_org
from modulo.core.license import verify_license_key
from modulo.core.runtime_config.key_bridge import get_public_url
from modulo.core.sanitize_log import sanitise_log_value
from modulo.db.crud.sso_provider import (
    get_enabled_saml_provider,
    get_provider_by_provider_id,
    list_enabled_oidc_providers,
)
from modulo.db.models.organisation import SYSTEM_ORG_ID, Organisation
from modulo.db.models.sso_provider import SsoProvider
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["sso"])

# Uniform generic error for unknown / disabled / org-inactive SAML provider
# slugs.  Same status, body and timing shape — a prober must not be able to
# distinguish an existing slug from a non-existing one (ADR 036 anti-enumeration).
_SAML_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Not Found",
)


def _frontend_url(settings: Settings) -> str:
    """Derive the frontend base URL (delegates to the shared resolver)."""
    from modulo.api.frontend_url import resolve_frontend_url

    return resolve_frontend_url(settings)


def _redirect_to_frontend(tokens: dict[str, str], settings: Settings) -> RedirectResponse:
    """Redirect the browser to the frontend callback URL.

    FAR-1197: only the short-lived access token stays in the URL fragment; the
    long-lived refresh token is delivered exclusively via the httpOnly
    ``modulo_refresh`` cookie (set on this API response) so it never lands in
    browser history or JS-reachable storage.
    """
    base = _frontend_url(settings)
    url = f"{base}/auth/callback#access_token={tokens['access_token']}"
    response = RedirectResponse(url=url)
    # Deferred import: keep the routes graph acyclic (same pattern as
    # _frontend_url).
    from modulo.api.routes.auth import _refresh_cookie_ttl, _set_csrf_cookie, _set_refresh_cookie

    refresh_token = tokens.get("refresh_token")
    if refresh_token:
        _set_refresh_cookie(response, refresh_token, settings)
        # The double-submit CSRF token must outlive the short access token so
        # the SPA can still present it on the cookie-only /auth/refresh call.
        _set_csrf_cookie(response, secrets.token_hex(32), settings, max_age_seconds=_refresh_cookie_ttl(settings))
    return response


# ---------------------------------------------------------------------------
# SSO callback failure -> frontend login page (FAR-1791)
# ---------------------------------------------------------------------------
# The IdP navigates the BROWSER to the callback routes, so a failure used to
# surface as a raw JSON page (FastAPI's HTTPException renderer) — e.g. a user
# who authenticated at the IdP but was never invited saw
# ``{"detail":"You have not been invited to this organisation."}``. Instead the
# routes RAISE :class:`SsoCallbackError` (a whitelisted code, never exception
# text), which the app's registered handler renders as a 303 back to the login
# page. Raising — rather than returning the redirect inline — is what lets the
# ``audited_system("saml_acs_attempted", ...)`` dependency observe the failure
# and record ``outcome="error"``. The real error is still logged server-side.
#
# The vocabulary + typed error live in ``modulo.auth.sso`` (next to the other
# SSO callback error, SsoProvisioningDeniedError) so there is ONE definition;
# the redirect target itself is built in the handler
# (``modulo.api.exception_handlers.sso_error_redirect``).


async def _resolve_org_slug_for_error(session: AsyncSession, org_id: uuid.UUID | None) -> str | None:
    """Best-effort org-slug lookup for the SSO error redirect target.

    Sends a denied user back to *their* org's login page. Any read failure
    (unbound RLS, missing row, DB error) returns ``None`` so the caller falls
    back to the base ``/login`` URL — the redirect must never be masked by a
    lookup fault on an already-failing path.
    """
    if org_id is None:
        return None
    try:
        if session.in_transaction():
            result = await session.execute(select(Organisation.slug).where(Organisation.id == org_id))
        else:
            async with session.begin():
                result = await session.execute(select(Organisation.slug).where(Organisation.id == org_id))
        return result.scalar_one_or_none()
    except Exception:
        _log.warning("sso.error_redirect_slug_lookup_failed", exc_info=True)
        return None


async def _run_sso_callback_process(
    session: AsyncSession,
    process: Callable[[], Awaitable[dict[str, str]]],
    *,
    log_context: str,
    contract_tag: str,
) -> dict[str, str]:
    """Run an SSO callback's token exchange, mapping every failure to a redirect.

    Shared by the OIDC callback and both SAML ACS routes (FAR-1791). Because the
    IdP navigates the BROWSER here, a failure must raise :class:`SsoCallbackError`
    (rendered as the 303 login redirect by ``sso_callback_error_handler``) rather
    than a raw JSON error page. The transaction is opened here so a
    ``session.begin()`` fault is mapped on the same path.

    Exception ordering is load-bearing and identical for every caller:
    ``SsoProvisioningDeniedError`` is a ``ValueError`` subclass, so its arm MUST
    precede ``except ValueError``; ``ProgrammingError`` is a ``SQLAlchemyError``,
    so it precedes its base.
    """
    try:
        async with session.begin():
            return await process()
    except SsoProvisioningDeniedError as exc:
        _log.warning("%s join denied: %s", log_context, sanitise_log_value(exc), exc_info=True)
        slug = await _resolve_org_slug_for_error(session, exc.org_id)
        raise SsoCallbackError(SSO_ERROR_NOT_INVITED, slug) from exc
    except ValueError as exc:
        _log.warning("%s failed: %s", log_context, sanitise_log_value(exc), exc_info=True)
        raise SsoCallbackError(SSO_ERROR_AUTH_FAILED) from exc
    except ProgrammingError as exc:
        _log.warning("%s failed — DB table missing: %s", log_context, exc, exc_info=True)
        raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, contract_tag)
        _log.warning("%s DB error: %s", log_context, exc, exc_info=True)
        raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("%s.unexpected_error", contract_tag)
        raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc


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
    2. The env-var SAML config is fully set (enabled + a VERIFIED licence key
       + metadata URL/XML).

    NOTE: SAML is single-provider-per-instance (``get_enabled_saml_provider``
    returns the first enabled provider globally via ``.limit(1)``).  This is
    therefore an instance-wide boolean — there is no per-org SAML support yet.

    FAR-1733: the env-var licence key is verified (signature + expiry), never
    trusted on mere presence — an unverifiable key does NOT enable the env
    SAML path (fail-closed; the DB-provider path is unaffected).
    """
    db_saml = await _get_enabled_saml_global(system_session, app_session)
    db_saml_ok = db_saml is not None and bool(db_saml.metadata_xml or db_saml.metadata_url)
    return db_saml_ok or (
        settings.modulo_saml_enabled
        and verify_license_key(getattr(settings, "modulo_license_key", "") or "") is not None
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

# FAR-1982: the login leg mints a per-request ``nonce`` (which the IdP binds
# into the ID token) and a PKCE ``code_verifier`` (whose S256 challenge travels
# in the authorize URL), and must carry both across the IdP round-trip to the
# callback. They are stored in a short-lived, httpOnly correlation cookie.
#
# Cookie-prefix decision (FAR-1982): the ``__Host-`` prefix is ADOPTED. A
# host-only cookie resists a sibling subdomain swapping in a ``Domain=`` cookie
# of the same name *while it exists*, but a subdomain sibling can set one when
# the victim has none (cookie tossing) and shadow the correlation value. The
# ``__Host-`` prefix closes that: the browser refuses to attach a ``Domain=``
# attribute to a ``__Host-``-named cookie (and requires ``Secure`` + ``Path=/``),
# so the only cookie that can satisfy this name is the host-only one this
# server set. The cost is exactly those mandatory attributes:
#   * ``Secure`` unconditionally — over plain HTTP a browser stores the cookie
#     only for a potentially-trustworthy origin (``localhost``/``127.0.0.1``).
#     A dev instance reached at a non-localhost HTTP host (LAN IP, custom
#     /etc/hosts name) therefore loses the cookie and the login fails CLOSED.
#     The repo's docker-compose local dev serves ``http://localhost:8000``
#     (potentially-trustworthy) and sets no ``DEBUG``, so the documented local
#     path is unaffected; ``Settings.debug`` (env ``DEBUG``) still relaxes
#     ``Secure`` on the post-login auth cookies, so a debug instance on a
#     non-localhost HTTP host keeps its session but cannot complete SSO login.
#   * ``Path=/`` — the cookie is sent on every same-host request for its 10-min
#     lifetime rather than only under ``/api/v1/auth/oidc``; a minor scope cost.
# SameSite is LAX (NOT Strict as the post-login auth cookies use): the
# IdP -> callback leg is a CROSS-SITE top-level GET navigation, on which Strict
# cookies are withheld, so a Strict cookie would break every legitimate login.
_OIDC_FLOW_COOKIE = "__Host-modulo_oidc_flow"
_OIDC_FLOW_TTL_SECONDS = 600  # 10 minutes, matching the SAML RelayState TTL


def _set_oidc_flow_cookie(
    response: Response,
    raw_state: str,
    nonce: str,
    code_verifier: str,
    settings: Settings,
) -> None:
    """Bind the request nonce + PKCE verifier to this browser (FAR-1982).

    The value is the HMAC-signed ``raw_state:nonce:code_verifier`` payload, so a
    cookie not written by this server cannot satisfy the callback's binding
    check. The signed ``state`` proves non-forgery; this cookie proves the
    callback belongs to THIS browser and carries the secrets the exchange needs.
    """
    response.set_cookie(
        key=_OIDC_FLOW_COOKIE,
        value=sign_state(f"{raw_state}:{nonce}:{code_verifier}", settings.secret_key),
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=_OIDC_FLOW_TTL_SECONDS,
        path="/",
    )


def _clear_oidc_flow_cookie(response: Response, settings: Settings) -> None:
    """Expire the correlation cookie once the flow has completed."""
    response.set_cookie(
        key=_OIDC_FLOW_COOKIE,
        value="",
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=0,
        path="/",
    )


def _read_oidc_flow_cookie(request: Request, state: str, settings: Settings) -> tuple[str, str] | None:
    """Return ``(nonce, code_verifier)`` for a bound flow, else ``None``.

    Fail closed on every malformed input: an absent/unverifiable cookie, a
    tampered state, a cookie whose ``raw_state`` does not match the returned
    state, or an empty nonce/verifier all return ``None`` and the caller raises
    an auth failure before any token exchange (FAR-1982).
    """
    state_data = verify_state(state, settings.secret_key)
    if not state_data or ":" not in state_data:
        return None
    # ``state`` is ``<provider_id>:<raw_state>`` (see oidc_get_authorize_url);
    # raw_state is a UUID, so rsplit isolates it even if a provider_id slug ever
    # contained a colon.
    raw_state = state_data.rsplit(":", 1)[-1]

    cookie_value = request.cookies.get(_OIDC_FLOW_COOKIE)
    if not cookie_value:
        return None
    cookie_data = verify_state(cookie_value, settings.secret_key)
    if not cookie_data:
        return None
    # ``raw_state``/``nonce``/``code_verifier`` all use URL-safe alphabets that
    # exclude ``:``, so a 2-split is unambiguous.
    parts = cookie_data.split(":", 2)
    if len(parts) != 3:
        return None
    cookie_raw_state, nonce, code_verifier = parts
    if not hmac.compare_digest(cookie_raw_state, raw_state):
        return None
    if not nonce or not code_verifier:
        return None
    return nonce, code_verifier


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
    public_url = get_public_url(settings).rstrip("/")
    redirect_uri = f"{public_url}/api/v1/auth/oidc/{provider}/callback"

    try:
        async with session.begin():
            auth_url, raw_state, nonce, code_verifier = await oidc_get_authorize_url(
                provider, settings, redirect_uri, system_session, session
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    response = Response(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": auth_url})
    # FAR-1982: carry the nonce + PKCE verifier to the callback in a
    # browser-bound, tossing-resistant cookie.
    _set_oidc_flow_cookie(response, raw_state, nonce, code_verifier, settings)
    return response


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
        # FAR-1791: a browser-facing callback failure redirects to the login
        # page with an error code rather than rendering a raw JSON page.
        raise SsoCallbackError(SSO_ERROR_AUTH_FAILED)

    # FAR-1982: the signed state only proves non-forgery — the nonce and PKCE
    # verifier that bind the ID token and the code exchange to THIS browser ride
    # in the login-time correlation cookie. Require it (fail closed) so a
    # callback replayed from another browser cannot complete.
    flow = _read_oidc_flow_cookie(request, state, settings)
    if flow is None:
        _log.warning("sso.oidc_flow_unbound provider=%s", sanitise_log_value(provider))
        raise SsoCallbackError(SSO_ERROR_AUTH_FAILED)
    expected_nonce, code_verifier = flow

    public_url = get_public_url(settings).rstrip("/")
    redirect_uri = f"{public_url}/api/v1/auth/oidc/{provider}/callback"

    tokens = await _run_sso_callback_process(
        session,
        partial(
            oidc_process_callback,
            code,
            state,
            settings,
            system_session,
            session,
            redirect_uri,
            expected_nonce=expected_nonce,
            code_verifier=code_verifier,
        ),
        log_context=f"OIDC callback for provider {sanitise_log_value(provider)}",
        contract_tag="sso.oidc_callback",
    )

    response = _redirect_to_frontend(tokens, settings)
    _clear_oidc_flow_cookie(response, settings)
    return response


# ---------------------------------------------------------------------------
# SAML 2.0 — per-provider routes (FAR-1001)
# ---------------------------------------------------------------------------


async def _resolve_saml_for_route(
    provider_id: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession,
) -> SsoProvider:
    """Resolve a SAML provider by slug for per-provider routes.

    Two-leg resolution (FAR-1001 / FAR-1058), fail-closed:
    1. System session (``modulo_system``, BYPASSRLS) — instance-global slug
       read; resolves a provider owned by ANY org.
    2. Fallback (system role unprovisioned, or slug not visible there): the
       app session (``modulo_app``, NOBYPASSRLS) scoped to the first org via
       ``_set_default_rls_org`` (single-org self-hosted behaviour).

    FAR-1058 fix: the app fallback previously called ``_set_default_rls_org``
    OUTSIDE any transaction on the app session — ``set_rls_org`` requires an
    active transaction, so every request reaching the fallback (an
    unknown/disabled slug in system-provisioned deployments; every slug in
    fallback deployments) raised ``RuntimeError`` → HTTP 500 instead of the
    uniform 404, breaking the ADR 036 anti-enumeration contract. The fallback
    read now runs inside a transaction: its own scoped one when the caller's
    app session has none (``set_config(..., is_local=true)`` reverts at
    commit, so the binding cannot leak into the caller's later transaction);
    in-place when the caller already opened one (the metadata route keeps its
    outer ``session.begin()``).

    Raises a uniform 404 for unknown / disabled / non-SAML slugs.
    Returns the resolved (enabled, SAML) provider row.
    """
    provider = await _read_system_saml_provider_by_slug(system_session, provider_id)
    if provider is None:
        if app_session.in_transaction():
            # Caller (e.g. the metadata route) already opened a transaction —
            # read in place so we never nest ``session.begin()``.
            await _set_default_rls_org(app_session)
            provider = await get_provider_by_provider_id(app_session, provider_id)
        else:
            async with app_session.begin():
                await _set_default_rls_org(app_session)
                provider = await get_provider_by_provider_id(app_session, provider_id)
    if provider is None or provider.provider_type != "saml" or not provider.enabled:
        raise _SAML_NOT_FOUND
    return provider


async def _saml_login_redirect(
    acs_url: str,
    settings: Settings,
    system_session: AsyncSession,
    session: AsyncSession,
    *,
    provider_id: str | None = None,
    relay_state: str | None = None,
    unexpected_error_tag: str,
) -> Response:
    """Generate the IdP redirect for a SAML login, mapping DB errors to HTTP errors.

    Shared by the legacy single-tenant ``/saml/login`` route and the
    per-provider ``/saml/{provider_id}/login`` route so the error-mapping
    contract (`ValueError` -> 400, missing table -> 501, DB error -> 503,
    unexpected -> 500) stays identical in both call sites.

    When ``relay_state`` is given (a signed HMAC token), it is embedded in
    the IdP redirect URL as the RelayState parameter — defense-in-depth
    for per-provider SAML.
    """
    try:
        async with session.begin():
            auth_url, _ = await saml_get_auth_url(
                settings,
                acs_url,
                system_session,
                session,
                provider_id=provider_id,
                relay_state=relay_state,
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.warning("SAML login failed — DB table missing: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "sso._saml_login_redirect")
        _log.warning("SAML login DB error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_ERROR_PLEASE_TRY,
        ) from exc
    except HTTPException:
        raise
    except Exception as e:
        _log.exception(unexpected_error_tag)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e

    return Response(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": auth_url})


async def _read_acs_form(request: Request) -> FormData:
    """Read a SAML ACS POST form, mapping a parse failure to the login redirect.

    FAR-1791: the IdP navigates the BROWSER to the ACS, so a body the parser
    rejects must not escape as an opaque JSON page. ``request.form()`` raises
    the BASE ``starlette.exceptions.HTTPException`` for a malformed multipart
    body, which ``handle_db_errors`` does not pass through (its arm catches the
    ``fastapi`` subclass), so an unguarded parse surfaces as a 500
    ``{"detail": "An unexpected error occurred."}`` — the very outcome this
    contract exists to avoid. A body we cannot parse carries no
    ``SAMLResponse``, which is exactly the missing-response case the callers
    already map to ``SSO_ERROR_AUTH_FAILED``; both now take the same 303 path.
    """
    try:
        return await request.form()
    except Exception as exc:
        _log.warning("sso.saml_acs_form_parse_failed", exc_info=True)
        raise SsoCallbackError(SSO_ERROR_AUTH_FAILED) from exc


# FAR-1516: PRE-AUTH route — the browser posts the IdP's SAML response with no
# session, and the identity is established inside the handler, so
# audited(principal_dep=...) has nothing to resolve. The actor-less variant
# records the ACS attempt with a SYSTEM actor: sentinel org until the provider
# resolves, then rebound to the provider's org. An SSO sign-in -> fail closed.
@router.post(
    "/saml/acs/{provider_id}",
    dependencies=[
        Depends(
            audited_system("saml_acs_attempted", "session", actor_source="pre_auth", fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        )
    ],
)
@handle_db_errors("sso.saml_acs_provider")
async def saml_acs_provider(
    provider_id: str,
    request: Request,
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> RedirectResponse:
    """Per-provider SAML ACS — the IdP posts back to this endpoint.

    Resolves the provider by its globally unique ``provider_id`` slug. The
    response is validated against THIS provider's SP entity ID and ACS URL.

    Because the IdP navigates the BROWSER here, a handled callback failure —
    including an unknown, disabled or non-SAML ``provider_id`` — is rendered
    as a 303 redirect to the login page carrying a whitelisted ``sso_error``
    code (FAR-1791), never the uniform JSON 404 the non-ACS routes keep.

    RelayState verification (FAR-1003, defense-in-depth):
    - valid signed RelayState for THIS provider → proceed
    - tampered / wrong provider / expired → reject (303 to the login page)
    - absent → do NOT fail; proceed on URL path (IdP-initiated SSO)

    Security reasoning: with routing by URL path, a forged RelayState
    cannot redirect an assertion to a different org's handler — that
    containment comes from the per-provider Entity ID + audience check
    (FAR-1010), NOT from RelayState. RelayState is an additional integrity
    signal, not the control.
    """
    bind_audit_org(request, SYSTEM_ORG_ID)
    try:
        provider = await _resolve_saml_for_route(provider_id, system_session, session)
    except HTTPException as exc:
        # FAR-1791: the ACS is browser-facing, so an unknown / disabled /
        # non-SAML slug must redirect to the login page rather than render the
        # uniform JSON 404 the non-ACS routes keep. The 303 is exactly as
        # opaque as the 404 (ADR 036): all three cases raise the SAME uniform
        # ``_SAML_NOT_FOUND`` above and map to the SAME code here, so nothing
        # new is disclosed.
        raise SsoCallbackError(SSO_ERROR_AUTH_FAILED) from exc
    except ProgrammingError as exc:
        # FAR-1791: a DB fault during provider resolution is the same outage
        # the process-response arms below already map to ``provider_unavailable``
        # — the browser must get the 303, not ``handle_db_errors``' raw 501 JSON.
        _log.warning("SAML ACS provider resolution failed — DB table missing: %s", exc, exc_info=True)
        raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc
    except SQLAlchemyError as exc:
        # Specific arms stay ahead of their bases: ``ProgrammingError`` above is
        # a ``SQLAlchemyError``, and a session-contract violation is delegated to
        # the shared classifier (500) exactly as the process-response arm does.
        raise_session_contract_error(exc, "sso.saml_acs_provider_resolution")
        _log.warning("SAML ACS provider resolution DB error: %s", exc, exc_info=True)
        raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc
    except Exception as exc:
        # FAR-1791: an UNEXPECTED (non-HTTP, non-DB) fault resolving the provider
        # must reach the browser as the same login-page 303 the process-response
        # arms give, never ``handle_db_errors``' raw JSON 500 — the resolution
        # path's contract is uniform with ``_run_sso_callback_process``'s
        # catch-all. Logged (not swallowed), so the real cause stays visible.
        _log.exception("sso.saml_acs_provider_resolution.unexpected_error")
        raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc
    # FAR-1516: the provider names its org — record the ACS attempt there (an
    # unknown slug redirects above and keeps the sentinel).
    bind_audit_org(request, provider.organisation_id)

    form = await _read_acs_form(request)
    raw_saml: object = form.get("SAMLResponse", "")
    if not isinstance(raw_saml, str) or not raw_saml:
        # FAR-1791: the IdP POSTs the browser here, so a failure redirects to
        # the login page (303 converts the POST to a GET) rather than
        # rendering a raw JSON page.
        raise SsoCallbackError(SSO_ERROR_AUTH_FAILED)

    # RelayState verification (FAR-1003): when present, verify HMAC signature
    # and provider_id match. Absence is tolerated — IdP-initiated SSO may
    # not carry RelayState, and routing is by the ACS URL path.
    raw_relay: object = form.get("RelayState", "")
    if isinstance(raw_relay, str) and raw_relay:
        relay_payload = verify_saml_relay_state(raw_relay, settings.secret_key)
        if relay_payload is None:
            _log.warning(
                "sso.saml_relay_state_invalid",
                extra={"provider_id": provider_id},
            )
            raise SsoCallbackError(SSO_ERROR_AUTH_FAILED)
        relay_pid = relay_payload.get("pid")
        if relay_pid != provider_id:
            _log.warning(
                "sso.saml_relay_state_provider_mismatch",
                extra={"expected": provider_id, "relay_pid": relay_pid},
            )
            raise SsoCallbackError(SSO_ERROR_AUTH_FAILED)

    tokens = await _run_sso_callback_process(
        session,
        partial(saml_process_response, raw_saml, settings, system_session, session, provider_id=provider_id),
        log_context=f"SAML ACS for provider {sanitise_log_value(provider_id)}",
        contract_tag="sso.saml_acs_provider",
    )

    return _redirect_to_frontend(tokens, settings)


@router.get("/saml/{provider_id}/metadata", response_class=PlainTextResponse)
@handle_db_errors("sso.saml_metadata_provider")
async def saml_metadata_provider(
    provider_id: str,
    _request: Request,
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> str:
    """Per-provider SP metadata — Entity ID and ACS Location are scoped to this provider."""
    try:
        async with session.begin():
            # Resolve provider globally (system session first, then app
            # fallback) — same two-leg resolution as the ACS/login routes
            # (FAR-1058: single shared helper; the app fallback runs inside
            # this outer transaction, so the RLS binding has a live tx).
            provider = await _resolve_saml_for_route(provider_id, system_session, session)

            entity_id = provider.entity_id or f"{get_public_url(settings).rstrip('/')}/api/v1/auth/saml/{provider_id}"
            public_url = get_public_url(settings).rstrip("/")
            acs_url = f"{public_url}/api/v1/auth/saml/acs/{provider_id}"

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
        _log.exception("sso.saml_metadata_provider.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_NO_PERIOD,
        ) from e


@router.get("/saml/{provider_id}/login")
@handle_db_errors("sso.saml_login_provider")
async def saml_login_provider(
    provider_id: str,
    _request: Request,
    _: object = require_feature_anonymous("sso"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    system_session: AsyncSession = Depends(get_system_db_session),
) -> Any:
    """Per-provider SAML login — redirects to the IdP for this specific provider.

    Embeds a HMAC-signed RelayState (FAR-1003) containing the provider_id
    and a timestamp. This is defense-in-depth: routing is by the ACS URL
    path, and RelayState absence must NOT fail (IdP-initiated SSO).
    """
    await _resolve_saml_for_route(provider_id, system_session, session)

    public_url = get_public_url(settings).rstrip("/")
    acs_url = f"{public_url}/api/v1/auth/saml/acs/{provider_id}"

    # Defense-in-depth (FAR-1003): sign the provider_id into RelayState.
    # Routing is by the ACS path — RelayState is an integrity signal, not
    # the routing key. Its absence is tolerated (IdP-initiated SSO).
    relay_state = sign_saml_relay_state(provider_id, settings.secret_key)

    return await _saml_login_redirect(
        acs_url,
        settings,
        system_session,
        session,
        provider_id=provider_id,
        relay_state=relay_state,
        unexpected_error_tag="sso.saml_login_provider.unexpected_error",
    )


# ---------------------------------------------------------------------------
# SAML 2.0 — legacy single-tenant convenience (unchanged)
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
    public_url = get_public_url(settings).rstrip("/")
    acs_url = f"{public_url}/api/v1/auth/saml/acs"

    return await _saml_login_redirect(
        acs_url,
        settings,
        system_session,
        session,
        unexpected_error_tag="sso.saml_login.unexpected_error",
    )


# FAR-1516: PRE-AUTH route — same gap as /saml/acs/{provider_id}: the assertion
# establishes the identity inside the handler, so no principal exists for
# audited(principal_dep=...) to resolve. Actor-less variant, SYSTEM actor.
@router.post(
    "/saml/acs",
    dependencies=[
        Depends(
            audited_system("saml_acs_attempted", "session", actor_source="pre_auth", fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        )
    ],
)
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
    bind_audit_org(request, SYSTEM_ORG_ID)
    # This legacy route has no provider slug in its path (saml_process_response
    # resolves the first enabled provider internally), so read it once here on
    # the SYSTEM session — the same instance-global leg that helper uses — to
    # attribute the attempt to the provider's org. Env-only config has no row,
    # so the sentinel published above stays.
    if system_session is not None and not system_session.in_transaction():
        try:
            async with system_session.begin():
                legacy_provider = await get_enabled_saml_provider(system_session)
        except ProgrammingError as exc:
            # FAR-1791: resolve-provider DB faults are browser-facing here too —
            # map them to the login-page 303, never ``handle_db_errors``' raw
            # 501/503 JSON. Specific arm ahead of its base (MRO).
            _log.warning("SAML ACS provider resolution failed — DB table missing: %s", exc, exc_info=True)
            raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc
        except SQLAlchemyError as exc:
            raise_session_contract_error(exc, "sso.saml_acs_resolution")
            _log.warning("SAML ACS provider resolution DB error: %s", exc, exc_info=True)
            raise SsoCallbackError(SSO_ERROR_PROVIDER_UNAVAILABLE) from exc
        if legacy_provider is not None:
            bind_audit_org(request, legacy_provider.organisation_id)
    form = await _read_acs_form(request)
    raw_saml: object = form.get("SAMLResponse", "")
    if not isinstance(raw_saml, str) or not raw_saml:
        # FAR-1791: browser-facing callback failure -> login page (see the
        # per-provider ACS route for the 303-over-POST rationale).
        raise SsoCallbackError(SSO_ERROR_AUTH_FAILED)

    tokens = await _run_sso_callback_process(
        session,
        partial(saml_process_response, raw_saml, settings, system_session, session),
        log_context="SAML ACS",
        contract_tag="sso.saml_acs",
    )

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

            public_url = get_public_url(settings).rstrip("/")
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
