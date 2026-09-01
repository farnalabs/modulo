"""OIDC and SAML 2.0 SSO support with JIT account provisioning."""

import base64
import hmac
import json
import logging
import urllib.parse
import uuid
from datetime import UTC, datetime

import httpx
from defusedxml import ElementTree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import create_access_token, create_refresh_token
from modulo.auth.oidc_verify import OidcVerifyError, verify_id_token
from modulo.auth.saml_handler import ModuloSamlAuth, SamlAuthError
from modulo.auth.secret_storage import decode_stored_secret
from modulo.core.ssrf import (
    derive_oidc_allowed_hosts,
    pinned_async_client,
    require_url_host_in_allowlist,
    validate_outbound_url_async,
    validate_outbound_url_preflight,
)
from modulo.db.crud.account import create_account, get_account_by_email, update_last_login
from modulo.db.crud.org_membership import create_membership, get_membership_by_account_and_org
from modulo.db.crud.sso_provider import get_enabled_saml_provider, get_provider_by_provider_id
from modulo.db.crud.team_membership import add_team_member, get_membership_by_team_and_account, update_member_role
from modulo.db.crud.token_family import create_family
from modulo.db.models.account import Account
from modulo.db.models.organisation import Organisation
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.rls import set_rls_org
from modulo.settings import Settings

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State signing (CSRF protection for OIDC redirect flow)
# ---------------------------------------------------------------------------


def sign_state(state: str, secret_key: str) -> str:
    """HMAC-SHA256 sign a state value for OIDC anti-forgery."""
    sig = hmac.new(secret_key.encode(), state.encode(), "sha256").hexdigest()[:16]
    return f"{state}:{sig}"


def verify_state(signed: str, secret_key: str) -> str | None:
    """Verify a signed state value. Returns the original on success, None on tamper."""
    parts = signed.rsplit(":", 1)
    if len(parts) != 2:
        return None
    state, sig = parts
    expected = hmac.new(secret_key.encode(), state.encode(), "sha256").hexdigest()[:16]
    if not hmac.compare_digest(expected, sig):
        return None
    return state


async def _set_default_rls_org(session: AsyncSession) -> None:
    """Set RLS org context to the first Organisation so pre-auth SSO routes can read the sso_providers table.

    The sso_providers table is OrgScoped, so Postgres RLS filters rows by
    ``app.organisation_id``. Pre-auth routes (no user/org claim) would otherwise
    get ZERO rows and silently fall through to the empty env fallback. We point
    RLS at the first org (single-org self-hosted assumption) so the global,
    org-unfiltered provider lookups resolve correctly.
    """
    result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
    org = result.scalar_one_or_none()
    if org is not None:
        await set_rls_org(session, org.id)


async def _scope_app_session_org(
    app_session: AsyncSession,
    db_provider: SsoProvider | None,
) -> uuid.UUID | None:
    """Scope the app session RLS for JIT writes and return the ``default_org_id``.

    The app session runs as ``modulo_app`` (NOBYPASSRLS), so the JIT writes
    (Account / OrgMembership) must be RLS-scoped to the org the user is being
    provisioned into. When a DB provider was resolved (its ``organisation_id``
    is the org owning that provider), scope to it and return it as the default
    org. Otherwise fall back to the first-org single-org behaviour (``None``
    tells ``jit_provision_user`` to resolve the first org itself).
    """
    if db_provider is not None and db_provider.organisation_id is not None:
        await set_rls_org(app_session, db_provider.organisation_id)
        return db_provider.organisation_id
    await _set_default_rls_org(app_session)
    return None


# ---------------------------------------------------------------------------
# JIT account provisioning
# ---------------------------------------------------------------------------


async def jit_provision_user(
    session: AsyncSession,
    settings: Settings,
    email: str,
    display_name: str,
    auth_provider: str,
    sso_subject: str,
    default_org_id: uuid.UUID | None = None,
) -> tuple[Account, uuid.UUID, str]:
    """Find or create an Account + OrgMembership for an SSO-authenticated identity.

    Returns (account, org_id, org_role). The target org is resolved BEFORE any
    ``create_account`` write so a zero-org deployment (env-path JIT, no provider
    org) raises a clean ``RuntimeError`` ("No organisation exists") instead of
    letting a raw RLS-scoped INSERT fail with a 500.
    """
    if default_org_id is not None:
        org_id = default_org_id
    else:
        result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
        org = result.scalar_one_or_none()
        if org is None:
            raise RuntimeError("No organisation exists — cannot JIT provision account")
        org_id = org.id

    account = await get_account_by_email(session, email)
    if account is not None:
        account.sso_subject = sso_subject
        account.auth_provider = auth_provider
        await session.flush()
    else:
        account = await create_account(
            session,
            email=email,
            display_name=display_name,
            password_hash=None,
            auth_provider=auth_provider,
        )
        account.sso_subject = sso_subject
        await session.flush()
        _log.info(
            "sso.jit_provisioned",
            extra={"email": email, "auth_provider": auth_provider, "sso_subject": sso_subject},
        )

    existing = await get_membership_by_account_and_org(session, account.id, org_id)
    if existing is None:
        membership = await create_membership(
            session,
            account_id=account.id,
            org_id=org_id,
            role=settings.modulo_sso_default_role,
        )
        org_role = membership.role
    else:
        org_role = existing.role

    return account, org_id, org_role


# ---------------------------------------------------------------------------
# Token issuance (same shape as existing LoginResponse)
# ---------------------------------------------------------------------------


async def apply_group_mappings(
    session: AsyncSession,
    account: Account,
    org_id: uuid.UUID,
    idp_groups: list[str],
    group_mappings: list[dict[str, str]],
) -> None:
    """Apply SSO group-to-team mappings for a JIT-provisioned account."""
    for mapping in group_mappings:
        if not isinstance(mapping, dict):
            _log.warning("sso.non_dict_mapping", extra={"mapping_type": type(mapping).__name__})
            continue
        idp_group = mapping.get("idp_group", "")
        if idp_group not in idp_groups:
            continue
        try:
            team_id = uuid.UUID(mapping["team_id"])
        except (ValueError, KeyError) as exc:
            _log.warning("sso.invalid_team_mapping", extra={"error": str(exc)})
            continue
        team_role = mapping.get("team_role", "viewer")

        existing = await get_membership_by_team_and_account(session, team_id, account.id)
        if existing is not None:
            if existing.role != team_role:
                await update_member_role(session, existing.id, team_role)
        else:
            await add_team_member(
                session,
                org_id=org_id,
                team_id=team_id,
                account_id=account.id,
                role=team_role,
            )


async def _lookup_provider_by_client_id(session: AsyncSession, client_id: str, org_id: uuid.UUID) -> SsoProvider | None:
    result = await session.execute(
        select(SsoProvider)
        .where(
            SsoProvider.client_id == client_id,
            SsoProvider.organisation_id == org_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _lookup_provider_by_entity_id(session: AsyncSession, entity_id: str, org_id: uuid.UUID) -> SsoProvider | None:
    result = await session.execute(
        select(SsoProvider)
        .where(
            SsoProvider.entity_id == entity_id,
            SsoProvider.organisation_id == org_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def issue_sso_tokens(
    account: Account, org_id: uuid.UUID, org_role: str, session: AsyncSession, settings: Settings
) -> dict[str, str]:
    """Issue access + refresh tokens for an SSO-authenticated account."""
    await update_last_login(session, account.id)
    family = await create_family(session, account.id, org_id)

    access_token = create_access_token(
        account.email,
        settings.secret_key,
        organisation_id=str(org_id),
        account_id=str(account.id),
        org_role=org_role,
        ttl_minutes=settings.modulo_access_token_minutes,
    )
    refresh_token = create_refresh_token(
        account.email,
        settings.secret_key,
        organisation_id=str(org_id),
        account_id=str(account.id),
        org_role=org_role,
        token_family=str(family.family_id),
        token_sequence=0,
    )
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}  # nosec B105 — OAuth token_type label, not a credential


# ---------------------------------------------------------------------------
# OIDC helpers
# ---------------------------------------------------------------------------


async def _read_system_oidc_provider(
    system_session: AsyncSession | None,
    provider_id: str,
) -> SsoProvider | None:
    """Global (cross-org) OIDC provider read via the ``modulo_system`` role.

    Returns ``None`` when the system role is unprovisioned (falls back to the
    regular engine running as ``modulo_app`` NOBYPASSRLS, which returns zero
    rows) or the provider genuinely does not exist.
    """
    if system_session is None:
        return None
    async with system_session.begin():
        return await get_provider_by_provider_id(system_session, provider_id)


async def _resolve_oidc_provider(
    provider_id: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    settings: Settings,
) -> tuple[str | None, str | None, str | None, list[str] | str | None, SsoProvider | None]:
    """Resolve OIDC IdP config: system (global) read, then app single-org, then env.

    The pre-auth SSO routes have no user/org claim. Provider config is
    instance-global (not tenant data), so the primary read goes through the
    ``modulo_system`` role (BYpassRLS) which sees every org. When the system
    read returns nothing (system role unprovisioned -> zero rows, or the
    provider genuinely absent), we fall back to the app session scoped to the
    first org (single-org self-hosted behaviour) before the env-var fallback.

    Returns ``(client_id, client_secret, discovery_url, scopes, db_provider)``.
    ``db_provider`` is the ``SsoProvider`` row when the config came from the DB
    (used for JIT org placement and SSRF validation); ``None`` for the env path.
    Returns all-``None`` when the provider is not configured at all.
    """
    db_provider = await _read_system_oidc_provider(system_session, provider_id)
    if db_provider is None and app_session is not None:
        await _set_default_rls_org(app_session)
        db_provider = await get_provider_by_provider_id(app_session, provider_id)
    if db_provider is not None and db_provider.provider_type == "oidc" and db_provider.enabled:
        discovery_url = db_provider.discovery_url
        if discovery_url:
            try:
                await validate_outbound_url_async(discovery_url)
            except ValueError as exc:
                raise ValueError(f"Rejected OIDC discovery_url for provider '{provider_id}': {exc}") from None
        return (
            db_provider.client_id,
            # NOSCOPE: instance-global OIDC IdP config, NOT tenant data. The
            # provider is read through the modulo_system role (BYPASSRLS) for
            # pre-auth SSO routes that carry no user/org claim, so there is no
            # per-request RLS org to bind the decrypt to — org-scoping here would
            # be a cross-context error, not a hardening gain. Kept on the pure
            # helper deliberately; the no-unscoped-decrypt gate allows it via the
            # nosemgrep below.
            # nosemgrep: no-unscoped-decrypt
            decode_stored_secret(db_provider.client_secret, settings.fernet_key) if db_provider.client_secret else None,
            discovery_url,
            json.loads(db_provider.scopes) if db_provider.scopes else None,
            db_provider,
        )

    providers = _parse_oidc_providers(settings)
    provider = next((p for p in providers if p["provider_id"] == provider_id), None)
    if not provider:
        return None, None, None, None, None
    return (
        provider["client_id"],
        provider["client_secret"],
        provider["discovery_url"],
        provider.get("scopes"),
        None,
    )


async def oidc_get_authorize_url(
    provider_id: str,
    settings: Settings,
    redirect_uri: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None = None,
) -> tuple[str, str]:
    """Build the OIDC authorization URL and return (url, raw_state).

    Resolves IdP config from the sso_providers DB table first (preferred, since
    the admin UI writes there); falls back to env-var providers for backward
    compatibility. Provider resolution uses the system session (``modulo_system``
    role, instance-global) so multi-org deployments resolve the correct provider
    regardless of org; the app session is used only for the single-org fallback.
    """
    client_id, _client_secret, discovery_url, scopes, _db_provider = await _resolve_oidc_provider(
        provider_id, system_session, app_session, settings
    )

    if client_id is None:
        raise ValueError(f"OIDC provider '{provider_id}' not configured")
    if not client_id or not discovery_url:
        raise ValueError(f"OIDC provider '{provider_id}' is missing client_id or discovery_url")

    try:
        if _db_provider is not None:
            disc = await _fetch_discovery_pinned(discovery_url)
        else:
            disc = await _fetch_discovery(discovery_url)
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to fetch discovery document: {exc}") from None
    auth_endpoint = disc.get("authorization_endpoint")
    if not isinstance(auth_endpoint, str) or not auth_endpoint:
        raise ValueError("No authorization_endpoint in discovery document")
    raw_issuer = disc.get("issuer")
    issuer_for_hosts = raw_issuer if isinstance(raw_issuer, str) else None
    _enforce_oidc_endpoint_host(auth_endpoint, discovery_url, issuer_for_hosts, "authorization")

    raw_state = str(uuid.uuid4())
    signed = sign_state(f"{provider_id}:{raw_state}", settings.secret_key)

    if isinstance(scopes, list):
        scope = " ".join(scopes) if scopes else "openid email profile"
    elif isinstance(scopes, str):
        scope = scopes.strip() or "openid email profile"
    else:
        scope = "openid email profile"
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": signed,
        }
    )
    return f"{auth_endpoint}?{params}", raw_state


async def oidc_process_callback(
    code: str,
    state: str,
    settings: Settings,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    redirect_uri: str,
) -> dict[str, str]:
    """Exchange auth code for tokens, JIT provision account, return JWT pair.

    Provider resolution uses the system session (``modulo_system`` role,
    instance-global) so the correct provider is resolved across orgs. JIT
    provisioning writes via the app session, RLS-scoped to the resolved
    provider's org (or the first-org fallback for the env/single-org path).
    """
    state_data = verify_state(state, settings.secret_key)
    if not state_data:
        _log.warning(
            "sso.csrf_state_mismatch", extra={"state_prefix": state[:20] + "..." if len(state) > 20 else state}
        )
        raise ValueError("Invalid state parameter — possible CSRF")

    provider_id = state_data.split(":", 1)[0] if ":" in state_data else state_data

    client_id, client_secret, discovery_url, _scopes, db_provider = await _resolve_oidc_provider(
        provider_id, system_session, app_session, settings
    )

    if client_id is None:
        raise ValueError(f"OIDC provider '{provider_id}' not found")
    if not client_id or not client_secret or not discovery_url:
        raise ValueError(f"OIDC provider '{provider_id}' is missing required configuration")

    try:
        if db_provider is not None:
            disc = await _fetch_discovery_pinned(discovery_url)
        else:
            disc = await _fetch_discovery(discovery_url)
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to fetch discovery document: {exc}") from None

    token_endpoint = disc.get("token_endpoint")
    if not isinstance(token_endpoint, str) or not token_endpoint:
        raise ValueError("No token_endpoint in discovery document")
    raw_issuer = disc.get("issuer")
    issuer_for_hosts = raw_issuer if isinstance(raw_issuer, str) else None
    _enforce_oidc_endpoint_host(token_endpoint, discovery_url, issuer_for_hosts, "token")

    try:
        token_data = await _exchange_code(
            token_endpoint,
            client_id,
            client_secret,
            code,
            redirect_uri,
        )
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to exchange authorization code: {exc}") from None

    id_token = token_data.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise ValueError("OIDC token response is missing a valid id_token")

    jwks_uri = disc.get("jwks_uri")
    issuer = disc.get("issuer")
    if not isinstance(jwks_uri, str) or not jwks_uri or not isinstance(issuer, str) or not issuer:
        raise ValueError(
            "OIDC provider discovery document is missing jwks_uri or issuer — "
            "cannot verify ID token signature. Check provider configuration."
        )
    _enforce_oidc_endpoint_host(jwks_uri, discovery_url, issuer, "jwks")

    try:
        claims = await verify_id_token(id_token, jwks_uri, client_id, issuer)
    except OidcVerifyError as exc:
        raise ValueError(str(exc)) from None

    email = claims.get("email", "") or claims.get("sub", "")
    if not email:
        raise ValueError("OIDC provider did not return an email or sub claim — cannot provision account")
    name = claims.get("name", "") or claims.get("preferred_username", "") or email.split("@")[0]
    sso_subject = f"{provider_id}:{claims.get('sub', email)}"

    if app_session is None:
        raise RuntimeError("No app session available for OIDC JIT provisioning")

    default_org_id = await _scope_app_session_org(app_session, db_provider)
    try:
        account, org_id, org_role = await jit_provision_user(
            app_session,
            settings,
            email,
            name,
            "oidc",
            sso_subject,
            default_org_id=default_org_id,
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None

    raw_groups = claims.get("groups", [])
    if not isinstance(raw_groups, list):
        raw_groups = []
    idp_groups: list[str] = raw_groups
    if idp_groups:
        db_provider_for_groups = await _lookup_provider_by_client_id(app_session, client_id, org_id)
        if db_provider_for_groups is not None and db_provider_for_groups.group_mappings:
            await apply_group_mappings(app_session, account, org_id, idp_groups, db_provider_for_groups.group_mappings)

    return await issue_sso_tokens(account, org_id, org_role, app_session, settings)


def _parse_oidc_providers(settings: Settings) -> list[dict[str, str]]:
    if not settings.modulo_oidc_providers:
        return []
    try:
        entries = json.loads(settings.modulo_oidc_providers)
    except (json.JSONDecodeError, TypeError) as exc:
        _log.warning("sso.oidc_invalid_json", extra={"error": str(exc)})
        return []
    if not isinstance(entries, list):
        _log.warning("sso.oidc_not_array", extra={"type": type(entries).__name__})
        return []
    valid = []
    required_fields = ("provider_id", "client_id", "client_secret", "discovery_url")
    for entry in entries:
        if isinstance(entry, dict) and not any(key not in entry for key in required_fields):
            valid.append(entry)
        else:
            safe_entry = (
                {key: value for key, value in entry.items() if key != "client_secret"}
                if isinstance(entry, dict)
                else {"invalid_type": type(entry).__name__}
            )
            _log.warning("sso.oidc_entry_missing_fields", extra={"entry": str(safe_entry)})
    return valid


# public alias for backwards compatibility
parse_oidc_providers = _parse_oidc_providers


def _require_json_object(value: object, context: str) -> dict[str, object]:
    """Validate and precisely type an object decoded from JSON."""
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")

    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{context} contains a non-string key")
        result[key] = item
    return result


async def _fetch_discovery(discovery_url: str) -> dict[str, object]:
    # Pinned client: SSRF-validates + pins the connect so the base discovery
    # fetch on the env-var path is protected exactly like the DB-provider path.
    async with await pinned_async_client(discovery_url) as client:
        resp = await client.get(discovery_url, timeout=httpx.Timeout(10.0, connect=5.0))
        resp.raise_for_status()
        try:
            decoded = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in discovery document: {exc}") from None
        return _require_json_object(decoded, "OIDC discovery document")


async def _fetch_discovery_pinned(discovery_url: str) -> dict[str, object]:
    async with await pinned_async_client(discovery_url) as client:
        resp = await client.get(discovery_url, timeout=httpx.Timeout(10.0, connect=5.0))
        resp.raise_for_status()
        try:
            decoded = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in discovery document: {exc}") from None
        return _require_json_object(decoded, "OIDC discovery document")


def _enforce_oidc_endpoint_host(url: str, discovery_url: str, issuer: str | None, purpose: str) -> None:
    """Reject a remote-supplied OIDC endpoint that points at an unsafe host.

    NOTE (FAR-506): the exact-host allowlist was an over-restrictive secondary
    boundary that broke legitimate multi-host IdPs. Google's real discovery
    document returns ``token_endpoint`` on oauth2.googleapis.com and
    ``jwks_uri`` on www.googleapis.com — sibling hosts of the discovery/issuer
    host (accounts.google.com). The **pinned client** is the real SSRF boundary:
    it pins the validated non-internal IP, closes the DNS-rebinding window, and
    blocks metadata/loopback. This check is therefore defense-in-depth only and
    rejects what a pinned client could never safely reach anyway — a non-HTTPS
    URL, an embedded-userinfo URL, or a literal-IP internal/loopback/metadata
    host. The discovery/issuer-host allowlist is kept as a preferred-log nicety:
    a cross-host (multi-host IdP) endpoint is logged as a warning, never
    rejected.
    """
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError(f"Rejected OIDC {purpose} endpoint: URL must use https:// scheme")
    try:
        validate_outbound_url_preflight(url)
    except ValueError as exc:
        raise ValueError(f"Rejected OIDC {purpose} endpoint: {exc}") from None

    # Preferred-log nicety: warn (but do NOT reject) when a derived endpoint lives
    # on a sibling host. Legitimate for multi-host IdPs (Google etc.).
    try:
        require_url_host_in_allowlist(url, derive_oidc_allowed_hosts(discovery_url, issuer))
    except ValueError:
        _log.warning("sso.oidc_endpoint_cross_host", extra={"purpose": purpose, "url": url})


async def _exchange_code(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, object]:
    # Pinned client: the token endpoint is a remote-supplied URL. Pinning
    # validates + pins the connect so the ``client_secret`` in the POST body is
    # only ever disclosed to a validated, allowlisted host — never a hostile or
    # internal/metadata target. The caller enforces the host allowlist before
    # calling here.
    async with await pinned_async_client(token_endpoint) as client:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        resp.raise_for_status()
        try:
            decoded = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in OIDC token response: {exc}") from None
        return _require_json_object(decoded, "OIDC token response")


def _decode_id_token_claims(id_token: str) -> dict[str, object]:
    """Decode ID token claims without signature verification.

    The code exchange with the token endpoint is over HTTPS (transport-level
    security), but this does not protect against a compromised token endpoint
    or a malicious IdP. In production, verify the JWT signature using the
    provider's JWKS endpoint and validate the ``iss`` and ``aud`` claims.
    """
    _log.warning("sso.id_token_no_verify")
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        pad = (4 - len(parts[1]) % 4) % 4
        padded = parts[1] + "=" * pad
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        return _require_json_object(decoded, "OIDC ID token claims")
    except ValueError as exc:
        _log.warning("sso.id_token_decode_failed", extra={"error": str(exc)})
        return {}


# ---------------------------------------------------------------------------
# SAML helpers
# ---------------------------------------------------------------------------


async def _read_system_saml_provider(system_session: AsyncSession | None) -> SsoProvider | None:
    """Global (cross-org) SAML provider read via the ``modulo_system`` role.

    Returns ``None`` when the system role is unprovisioned (returns zero rows)
    or no enabled SAML provider exists. Pre-auth SAML is a single-provider flow,
    so the first enabled provider globally is the one used.
    """
    if system_session is None:
        return None
    async with system_session.begin():
        return await get_enabled_saml_provider(system_session)


async def _resolve_saml_config(
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
    settings: Settings,
) -> tuple[str, str, str | None, str | None, SsoProvider | None]:
    """Resolve SAML IdP config: system (global) read, then app single-org, then env.

    The pre-auth SAML routes have no user/org claim. IdP config is
    instance-global, so the primary read goes through the ``modulo_system`` role
    (BYPASSRLS) which sees every org. When the system read returns nothing, fall
    back to the app session scoped to the first org (single-org self-hosted
    behaviour) before the env-var fallback.

    Returns ``(idp_metadata_xml, entity_id, sp_private_key, sp_x509_cert,
    db_provider)``. ``entity_id`` is ``db_saml.entity_id or
    settings.modulo_saml_entity_id`` (used by the SP handler). ``db_provider`` is
    the row when config came from the DB (used for JIT org placement); ``None``
    for the env path.

    Admin-configured ``metadata_url`` (DB path) is SSRF-validated and fetched
    with explicit error handling; the env path is fetched with the same pinned
    client (FAR-517) so the connect is pinned to the validated address rather
    than re-resolved at request time.
    """
    db_saml = await _read_system_saml_provider(system_session)
    if db_saml is None and app_session is not None:
        await _set_default_rls_org(app_session)
        db_saml = await get_enabled_saml_provider(app_session)
    if db_saml is not None:
        idp_metadata = db_saml.metadata_xml or None
        if not idp_metadata and db_saml.metadata_url:
            try:
                await validate_outbound_url_async(db_saml.metadata_url)
            except ValueError as exc:
                raise ValueError(f"Rejected SAML metadata_url for provider: {exc}") from None
            try:
                async with await pinned_async_client(db_saml.metadata_url) as client:
                    resp = await client.get(db_saml.metadata_url, timeout=httpx.Timeout(15.0, connect=5.0))
                    resp.raise_for_status()
                    idp_metadata = resp.text
            except httpx.HTTPError as exc:
                raise ValueError("Failed to fetch SAML IdP metadata from provider metadata_url") from exc
        if not idp_metadata:
            raise ValueError("SAML provider is missing IdP metadata (set metadata_xml or metadata_url)")
        entity_id = db_saml.entity_id or settings.modulo_saml_entity_id or "modulo"
        sp_key = settings.modulo_saml_sp_private_key or None
        sp_cert = settings.modulo_saml_sp_x509_cert or None
        return idp_metadata, entity_id, sp_key, sp_cert, db_saml

    if not settings.modulo_saml_enabled:
        raise ValueError("SAML is not enabled")
    if not settings.modulo_license_key:
        raise ValueError("SAML requires a license key (Team feature)")

    try:
        idp_metadata = await _saml_fetch_idp_metadata(settings)
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"Failed to fetch IdP metadata: {exc}") from None

    return (
        idp_metadata,
        settings.modulo_saml_entity_id,
        settings.modulo_saml_sp_private_key or None,
        settings.modulo_saml_sp_x509_cert or None,
        None,
    )


async def saml_get_auth_url(
    settings: Settings,
    acs_url: str,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None = None,
) -> tuple[str, str]:
    """Generate a SAML AuthnRequest using python3-saml and return (IdP redirect URL, _).

    python3-saml handles proper XML construction, signing (if SP key configured),
    and encoding. The second return value (request_id) is no longer used by the
    caller but kept for API compatibility.

    Resolves IdP config through the sso_providers DB table first (preferred, since
    the admin UI writes there); falls back to env-var config for backward
    compatibility. Provider resolution uses the system session (``modulo_system``
    role, instance-global) so multi-org deployments resolve the correct IdP.
    """
    idp_metadata, entity_id, sp_key, sp_cert, _db_saml = await _resolve_saml_config(
        system_session, app_session, settings
    )
    handler = ModuloSamlAuth(
        entity_id=entity_id,
        acs_url=acs_url,
        idp_metadata_xml=idp_metadata,
        sp_private_key=sp_key,
        sp_x509_cert=sp_cert,
    )
    try:
        auth_url = handler.get_auth_url()
    except Exception as exc:
        raise ValueError(f"Failed to generate SAML AuthnRequest: {exc}") from None
    return auth_url, ""


def _decode_saml_response(saml_response: str) -> bytes:
    """Decode a base64 SAML Response, normalising padding for urlsafe input.

    Raises ValueError on base64 decode failure (mirrors the existing error
    contract so the ACS route returns 401).
    """
    try:
        return base64.b64decode(saml_response, validate=False)
    except ValueError as exc:
        raise ValueError(f"Invalid base64 SAML response: {exc}") from None


def _validate_saml_response_destination(saml_response: str, acs_url: str) -> None:
    """Validate the SAML Response ``Destination`` matches the configured ACS URL.

    SAML 2.0 Core §4.1.1 requires the Response ``Destination`` to match the
    SP's ACS endpoint. A mismatched Destination is a replay/misdelivery signal
    and MUST be rejected. The attribute may be legitimately absent from some
    IdPs, in which case validation is skipped (python3-saml enforces it only
    in strict mode, which is not enabled here).
    """
    try:
        raw = _decode_saml_response(saml_response)
    except ValueError:
        return
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return
    destination = root.get("Destination")
    if destination is None:
        return
    if destination.rstrip("/") != acs_url.rstrip("/"):
        _log.warning(
            "sso.saml_destination_mismatch",
            extra={"expected": acs_url, "actual": destination},
        )
        raise ValueError("SAML response Destination does not match the ACS URL")


async def saml_process_response(
    saml_response: str,
    settings: Settings,
    system_session: AsyncSession | None,
    app_session: AsyncSession | None,
) -> dict[str, str]:
    """Validate a SAML Response using python3-saml and issue tokens.

    python3-saml provides XML signature verification using the IdP's X.509
    certificate from metadata (the critical security gap in the old
    implementation), plus condition validation, audience restriction, and
    clock-skew management.

    Resolves IdP metadata from the sso_providers DB table first (preferred, since
    the admin UI writes there); falls back to env-var config for backward
    compatibility. Provider resolution uses the system session (``modulo_system``
    role, instance-global); JIT provisioning writes via the app session,
    RLS-scoped to the resolved provider's org (or first-org fallback).
    """
    idp_metadata, entity_id, sp_key, sp_cert, db_saml = await _resolve_saml_config(
        system_session, app_session, settings
    )

    try:
        _, idp_entity_id = _saml_parse_idp_metadata(idp_metadata)
    except (ElementTree.ParseError, ValueError) as exc:
        raise ValueError(f"Failed to parse IdP metadata: {exc}") from None

    acs_url = f"{settings.modulo_public_url.rstrip('/')}/api/v1/auth/saml/acs"
    _validate_saml_response_destination(saml_response, acs_url)
    handler = ModuloSamlAuth(
        entity_id=entity_id,
        acs_url=acs_url,
        idp_metadata_xml=idp_metadata,
        sp_private_key=sp_key,
        sp_x509_cert=sp_cert,
    )
    try:
        result = handler.process_response(saml_response)
    except SamlAuthError as exc:
        _log.warning("sso.saml_signature_validation_failed", extra={"error": str(exc)})
        raise ValueError(str(exc)) from None

    name_id = result["name_id"]
    raw_attrs = result["attributes"]
    attrs = {attr_name: ",".join(values) for attr_name, values in raw_attrs.items()}

    email = attrs.get("email", "") or attrs.get("Email", "") or name_id or ""
    if not email:
        raise ValueError("SAML provider did not return an email attribute — cannot provision account")
    display_name = (
        attrs.get("displayName", "")
        or attrs.get("cn", "")
        or attrs.get("firstName", "")
        or (email.split("@")[0] if "@" in email else email)
    )
    sso_subject = f"saml:{idp_entity_id}:{name_id}"

    if app_session is None:
        raise RuntimeError("No app session available for SAML JIT provisioning")

    default_org_id = await _scope_app_session_org(app_session, db_saml)
    try:
        account, org_id, org_role = await jit_provision_user(
            app_session,
            settings,
            email,
            display_name,
            "saml",
            sso_subject,
            default_org_id=default_org_id,
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None

    saml_groups: list[str] = []
    for group_attr in ("groups", "memberOf", "Group"):
        raw = attrs.get(group_attr, "")
        if raw:
            saml_groups = [g.strip() for g in raw.split(",") if g.strip()]
            break
    if saml_groups:
        db_provider = await _lookup_provider_by_entity_id(app_session, idp_entity_id, org_id)
        if db_provider is not None and db_provider.group_mappings:
            await apply_group_mappings(app_session, account, org_id, saml_groups, db_provider.group_mappings)

    try:
        return await issue_sso_tokens(account, org_id, org_role, app_session, settings)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None


def _parse_saml_datetime(value: str) -> datetime:
    """Parse a SAML timestamp, handling both timezone-aware and naive formats.

    SAML 2.0 timestamps SHOULD include timezone (``Z`` suffix or offset),
    but some IdPs omit it. We treat a naive timestamp as UTC.
    """
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def _saml_fetch_idp_metadata(settings: Settings) -> str:
    if settings.modulo_saml_idp_metadata_xml:
        return settings.modulo_saml_idp_metadata_xml
    if settings.modulo_saml_idp_metadata_url:
        async with await pinned_async_client(settings.modulo_saml_idp_metadata_url) as client:
            resp = await client.get(settings.modulo_saml_idp_metadata_url, timeout=httpx.Timeout(15.0, connect=5.0))
            resp.raise_for_status()
            return resp.text
    raise ValueError("SAML IdP metadata not configured (set MODULO_SAML_IDP_METADATA_URL or _XML)")


def _saml_parse_idp_metadata(
    xml_str: str,
) -> tuple[str, str]:
    """Parse IdP metadata XML. Returns (sso_url, entity_id)."""
    root = ElementTree.fromstring(xml_str)
    md_ns = "urn:oasis:names:tc:SAML:2.0:metadata"

    entity_id = root.get("entityID", "")

    sso_descriptor = root.find(f"{{{md_ns}}}IDPSSODescriptor")
    if sso_descriptor is None:
        raise ValueError("No IDPSSODescriptor in IdP metadata")

    sso_service = sso_descriptor.find(
        f"{{{md_ns}}}SingleSignOnService[@Binding='urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect']"
    )
    if sso_service is None:
        sso_service = sso_descriptor.find(f"{{{md_ns}}}SingleSignOnService")
    sso_url = sso_service.get("Location", "") if sso_service is not None else ""
    if not sso_url:
        raise ValueError("No SAML SingleSignOnService with Location found in IdP metadata")

    return sso_url, entity_id
