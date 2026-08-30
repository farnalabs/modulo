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
from modulo.db.crud.account import create_account, get_account_by_email, update_last_login
from modulo.db.crud.org_membership import create_membership, get_membership_by_account_and_org
from modulo.db.crud.team_membership import add_team_member, get_membership_by_team_and_account, update_member_role
from modulo.db.crud.token_family import create_family
from modulo.db.models.account import Account
from modulo.db.models.organisation import Organisation
from modulo.db.models.sso_provider import SsoProvider
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

    Returns (account, org_id, org_role).
    """
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

    if default_org_id is not None:
        org_id = default_org_id
    else:
        result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
        org = result.scalar_one_or_none()
        if org is None:
            raise RuntimeError("No organisation exists — cannot JIT provision account")
        org_id = org.id

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


async def oidc_get_authorize_url(
    provider_id: str,
    settings: Settings,
    redirect_uri: str,
) -> tuple[str, str]:
    """Build the OIDC authorization URL and return (url, raw_state)."""
    providers = _parse_oidc_providers(settings)
    provider = next((p for p in providers if p["provider_id"] == provider_id), None)
    if not provider:
        raise ValueError(f"OIDC provider '{provider_id}' not configured")

    try:
        disc = await _fetch_discovery(provider["discovery_url"])
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to fetch discovery document: {exc}") from None
    auth_endpoint = disc.get("authorization_endpoint")
    if not isinstance(auth_endpoint, str) or not auth_endpoint:
        raise ValueError("No authorization_endpoint in discovery document")

    raw_state = str(uuid.uuid4())
    signed = sign_state(f"{provider_id}:{raw_state}", settings.secret_key)

    params = urllib.parse.urlencode(
        {
            "client_id": provider["client_id"],
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": redirect_uri,
            "state": signed,
        }
    )
    return f"{auth_endpoint}?{params}", raw_state


async def oidc_process_callback(
    code: str,
    state: str,
    settings: Settings,
    session: AsyncSession,
    redirect_uri: str,
) -> dict[str, str]:
    """Exchange auth code for tokens, JIT provision account, return JWT pair."""
    state_data = verify_state(state, settings.secret_key)
    if not state_data:
        _log.warning(
            "sso.csrf_state_mismatch", extra={"state_prefix": state[:20] + "..." if len(state) > 20 else state}
        )
        raise ValueError("Invalid state parameter — possible CSRF")

    provider_id = state_data.split(":", 1)[0] if ":" in state_data else state_data
    providers = _parse_oidc_providers(settings)
    provider = next((p for p in providers if p["provider_id"] == provider_id), None)
    if not provider:
        raise ValueError(f"OIDC provider '{provider_id}' not found")

    try:
        disc = await _fetch_discovery(provider["discovery_url"])
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to fetch discovery document: {exc}") from None

    token_endpoint = disc.get("token_endpoint")
    if not isinstance(token_endpoint, str) or not token_endpoint:
        raise ValueError("No token_endpoint in discovery document")

    try:
        token_data = await _exchange_code(
            token_endpoint,
            provider["client_id"],
            provider["client_secret"],
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

    try:
        claims = await verify_id_token(id_token, jwks_uri, provider["client_id"], issuer)
    except OidcVerifyError as exc:
        raise ValueError(str(exc)) from None

    email = claims.get("email", "") or claims.get("sub", "")
    if not email:
        raise ValueError("OIDC provider did not return an email or sub claim — cannot provision account")
    name = claims.get("name", "") or claims.get("preferred_username", "") or email.split("@")[0]
    sso_subject = f"{provider_id}:{claims.get('sub', email)}"

    try:
        account, org_id, org_role = await jit_provision_user(session, settings, email, name, "oidc", sso_subject)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from None

    raw_groups = claims.get("groups", [])
    if not isinstance(raw_groups, list):
        raw_groups = []
    idp_groups: list[str] = raw_groups
    if idp_groups:
        db_provider = await _lookup_provider_by_client_id(session, provider["client_id"], org_id)
        if db_provider is not None and db_provider.group_mappings:
            await apply_group_mappings(session, account, org_id, idp_groups, db_provider.group_mappings)

    return await issue_sso_tokens(account, org_id, org_role, session, settings)


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
    async with httpx.AsyncClient() as client:
        resp = await client.get(discovery_url, timeout=httpx.Timeout(10.0, connect=5.0))
        resp.raise_for_status()
        try:
            decoded = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in discovery document: {exc}") from None
        return _require_json_object(decoded, "OIDC discovery document")


async def _exchange_code(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, object]:
    async with httpx.AsyncClient() as client:
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


async def saml_get_auth_url(
    settings: Settings,
    acs_url: str,
) -> tuple[str, str]:
    """Generate a SAML AuthnRequest using python3-saml and return (IdP redirect URL, _).

    python3-saml handles proper XML construction, signing (if SP key configured),
    and encoding. The second return value (request_id) is no longer used by the
    caller but kept for API compatibility.
    """
    if not settings.modulo_saml_enabled:
        raise ValueError("SAML is not enabled")
    if not settings.modulo_license_key:
        raise ValueError("SAML requires a license key (Team feature)")

    try:
        idp_metadata = await _saml_fetch_idp_metadata(settings)
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"Failed to fetch IdP metadata: {exc}") from None

    handler = ModuloSamlAuth(
        entity_id=settings.modulo_saml_entity_id,
        acs_url=acs_url,
        idp_metadata_xml=idp_metadata,
        sp_private_key=settings.modulo_saml_sp_private_key or None,
        sp_x509_cert=settings.modulo_saml_sp_x509_cert or None,
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
    session: AsyncSession,
) -> dict[str, str]:
    """Validate a SAML Response using python3-saml and issue tokens.

    python3-saml provides XML signature verification using the IdP's X.509
    certificate from metadata (the critical security gap in the old
    implementation), plus condition validation, audience restriction, and
    clock-skew management.
    """
    if not settings.modulo_saml_enabled:
        raise ValueError("SAML is not enabled")
    if not settings.modulo_license_key:
        raise ValueError("SAML requires a license key (Team feature)")

    try:
        idp_metadata = await _saml_fetch_idp_metadata(settings)
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"Failed to fetch IdP metadata: {exc}") from None

    try:
        _, idp_entity_id = _saml_parse_idp_metadata(idp_metadata)
    except (ElementTree.ParseError, ValueError) as exc:
        raise ValueError(f"Failed to parse IdP metadata: {exc}") from None

    acs_url = f"{settings.modulo_public_url.rstrip('/')}/api/v1/auth/saml/acs"
    _validate_saml_response_destination(saml_response, acs_url)
    handler = ModuloSamlAuth(
        entity_id=settings.modulo_saml_entity_id,
        acs_url=acs_url,
        idp_metadata_xml=idp_metadata,
        sp_private_key=settings.modulo_saml_sp_private_key or None,
        sp_x509_cert=settings.modulo_saml_sp_x509_cert or None,
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

    try:
        account, org_id, org_role = await jit_provision_user(
            session, settings, email, display_name, "saml", sso_subject
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
        db_provider = await _lookup_provider_by_entity_id(session, idp_entity_id, org_id)
        if db_provider is not None and db_provider.group_mappings:
            await apply_group_mappings(session, account, org_id, saml_groups, db_provider.group_mappings)

    try:
        return await issue_sso_tokens(account, org_id, org_role, session, settings)
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
        async with httpx.AsyncClient() as client:
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
