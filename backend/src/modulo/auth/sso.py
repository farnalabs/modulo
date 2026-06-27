"""OIDC and SAML 2.0 SSO support with JIT user provisioning."""

import base64
import hmac
import json
import logging
import urllib.parse
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

import defusedxml.ElementTree as ET
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import create_access_token, create_refresh_token
from modulo.auth.oidc_verify import OidcVerifyError, verify_id_token
from modulo.db.crud.team_membership import add_team_member, get_membership_by_team_and_user, update_member_role
from modulo.db.crud.token_family import create_family
from modulo.db.crud.user import get_user_by_email, update_last_login
from modulo.db.models.organisation import Organisation
from modulo.db.models.sso_provider import SsoProvider
from modulo.db.models.user import User
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
# JIT user provisioning
# ---------------------------------------------------------------------------


async def jit_provision_user(
    session: AsyncSession,
    settings: Settings,
    email: str,
    display_name: str,
    auth_provider: str,
    sso_subject: str,
    default_org_id: uuid.UUID | None = None,
) -> User:
    """Find or create a user record for an SSO-authenticated identity."""
    user = await get_user_by_email(session, email)
    if user is not None:
        user.sso_subject = sso_subject
        user.auth_provider = auth_provider
        await session.flush()
        return user

    if default_org_id is not None:
        org_id = default_org_id
    else:
        result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
        org = result.scalar_one_or_none()
        if org is None:
            raise RuntimeError("No organisation exists — cannot JIT provision user")
        org_id = org.id

    user = User(
        organisation_id=org_id,
        email=email,
        display_name=display_name,
        password_hash=None,
        org_role=settings.modulo_sso_default_role,
        auth_provider=auth_provider,
        sso_subject=sso_subject,
    )
    session.add(user)
    await session.flush()
    _log.info(
        "sso.jit_provisioned",
        extra={"email": email, "auth_provider": auth_provider, "sso_subject": sso_subject},
    )
    return user


# ---------------------------------------------------------------------------
# Token issuance (same shape as existing LoginResponse)
# ---------------------------------------------------------------------------


async def apply_group_mappings(
    session: AsyncSession,
    user: User,
    idp_groups: list[str],
    group_mappings: list[dict[str, str]],
) -> None:
    """Apply SSO group-to-team mappings for a JIT-provisioned user."""
    for mapping in group_mappings:
        idp_group = mapping.get("idp_group", "")
        if idp_group not in idp_groups:
            continue
        team_id = uuid.UUID(mapping["team_id"])
        team_role = mapping.get("team_role", "viewer")

        existing = await get_membership_by_team_and_user(session, team_id, user.id)
        if existing is not None:
            if existing.role != team_role:
                await update_member_role(session, existing.id, team_role)
        else:
            await add_team_member(
                session,
                org_id=user.organisation_id,
                team_id=team_id,
                user_id=user.id,
                role=team_role,
            )


async def _lookup_provider_by_client_id(session: AsyncSession, client_id: str) -> SsoProvider | None:
    from sqlalchemy import select

    result = await session.execute(select(SsoProvider).where(SsoProvider.client_id == client_id).limit(1))
    return result.scalar_one_or_none()


async def _lookup_provider_by_entity_id(session: AsyncSession, entity_id: str) -> SsoProvider | None:
    from sqlalchemy import select

    result = await session.execute(select(SsoProvider).where(SsoProvider.entity_id == entity_id).limit(1))
    return result.scalar_one_or_none()


async def issue_sso_tokens(user: User, session: AsyncSession, settings: Settings) -> dict[str, str]:
    """Issue access + refresh tokens for an SSO-authenticated user."""
    await update_last_login(session, user.id)
    family = await create_family(session, user.id, user.organisation_id)

    access_token = create_access_token(
        user.email,
        settings.secret_key,
        organisation_id=str(user.organisation_id),
        user_id=str(user.id),
        org_role=user.org_role,
    )
    refresh_token = create_refresh_token(
        user.email,
        settings.secret_key,
        organisation_id=str(user.organisation_id),
        user_id=str(user.id),
        org_role=user.org_role,
        token_family=str(family.family_id),
        token_sequence=0,
    )
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}


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

    disc = await _fetch_discovery(provider["discovery_url"])
    auth_endpoint = disc.get("authorization_endpoint")
    if not auth_endpoint:
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
    """Exchange auth code for tokens, JIT provision user, return JWT pair."""
    state_data = verify_state(state, settings.secret_key)
    if not state_data:
        raise ValueError("Invalid state parameter — possible CSRF")

    provider_id = state_data.split(":", 1)[0] if ":" in state_data else state_data
    providers = _parse_oidc_providers(settings)
    provider = next((p for p in providers if p["provider_id"] == provider_id), None)
    if not provider:
        raise ValueError(f"OIDC provider '{provider_id}' not found")

    disc = await _fetch_discovery(provider["discovery_url"])
    token_endpoint = disc.get("token_endpoint")
    if not token_endpoint:
        raise ValueError("No token_endpoint in discovery document")

    token_data = await _exchange_code(
        token_endpoint,
        provider["client_id"],
        provider["client_secret"],
        code,
        redirect_uri,
    )

    id_token = token_data.get("id_token", "")

    jwks_uri = disc.get("jwks_uri", "")
    issuer = disc.get("issuer", "")
    if jwks_uri and issuer:
        try:
            claims = await verify_id_token(id_token, jwks_uri, provider["client_id"], issuer)
        except OidcVerifyError as exc:
            raise ValueError(str(exc)) from None
    else:
        claims = _decode_id_token_claims(id_token)
        _log.warning("sso.oidc_no_discovery_metadata", extra={"provider_id": provider_id})

    email = claims.get("email", "") or claims.get("sub", "")
    name = claims.get("name", "") or claims.get("preferred_username", "") or email.split("@")[0]
    sso_subject = f"{provider_id}:{claims.get('sub', email)}"

    user = await jit_provision_user(session, settings, email, name, "oidc", sso_subject)

    idp_groups: list[str] = claims.get("groups", []) or []
    if idp_groups:
        db_provider = await _lookup_provider_by_client_id(session, provider["client_id"])
        if db_provider is not None and db_provider.group_mappings:
            await apply_group_mappings(session, user, idp_groups, db_provider.group_mappings)

    return await issue_sso_tokens(user, session, settings)


def parse_oidc_providers(settings: Settings) -> list[dict[str, str]]:
    """Parse MODULO_OIDC_PROVIDERS JSON to a list of provider dicts."""
    return _parse_oidc_providers(settings)


def _parse_oidc_providers(settings: Settings) -> list[dict[str, str]]:
    if not settings.modulo_oidc_providers:
        return []
    try:
        entries = json.loads(settings.modulo_oidc_providers)
    except (json.JSONDecodeError, TypeError):
        _log.warning("sso.oidc_invalid_json")
        return []
    valid = []
    for entry in entries:
        if all(k in entry for k in ("provider_id", "client_id", "client_secret", "discovery_url")):
            valid.append(entry)
        else:
            _log.warning("sso.oidc_entry_missing_fields", extra={"entry": str(entry)})
    return valid


async def _fetch_discovery(discovery_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(discovery_url, timeout=10)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


async def _exchange_code(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
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
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


def _decode_id_token_claims(id_token: str) -> dict[str, Any]:
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
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))  # type: ignore[no-any-return]
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# SAML helpers
# ---------------------------------------------------------------------------


async def saml_get_auth_url(
    settings: Settings,
    acs_url: str,
) -> tuple[str, str]:
    """Generate a SAML AuthnRequest and return (IdP redirect URL, request_id)."""
    if not settings.modulo_saml_enabled:
        raise ValueError("SAML is not enabled")
    if not settings.modulo_license_key:
        raise ValueError("SAML requires a license key (enterprise feature)")

    idp_metadata = await _saml_fetch_idp_metadata(settings)
    idp_sso_url, _ = _saml_parse_idp_metadata(idp_metadata)

    request_id = f"_{uuid.uuid4().hex}"
    issue_instant = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    authn_request_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<samlp:AuthnRequest"
        ' xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
        ' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
        f' ID="{request_id}"'
        ' Version="2.0"'
        f' IssueInstant="{issue_instant}"'
        f' Destination="{idp_sso_url}"'
        f' AssertionConsumerServiceURL="{acs_url}"'
        ' ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        f" <saml:Issuer>{settings.modulo_saml_entity_id}</saml:Issuer>"
        " <samlp:NameIDPolicy"
        '  Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"'
        '  AllowCreate="true"/>'
        "</samlp:AuthnRequest>"
    )

    deflated = zlib.compress(authn_request_xml.encode())[2:-4]
    encoded = base64.b64encode(deflated).decode()
    params = urllib.parse.urlencode({"SAMLRequest": encoded})
    return f"{idp_sso_url}?{params}", request_id


async def saml_process_response(
    saml_response: str,
    settings: Settings,
    session: AsyncSession,
) -> dict[str, str]:
    """Validate a SAML Response and issue tokens."""
    if not settings.modulo_saml_enabled:
        raise ValueError("SAML is not enabled")
    if not settings.modulo_license_key:
        raise ValueError("SAML requires a license key (enterprise feature)")

    idp_metadata = await _saml_fetch_idp_metadata(settings)
    _, idp_entity_id = _saml_parse_idp_metadata(idp_metadata)

    decoded = base64.b64decode(saml_response).decode()
    root = ET.fromstring(decoded)
    ns = {
        "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
        "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    }

    assertion = root.find(".//saml:Assertion", ns)
    if assertion is None:
        raise ValueError("No SAML Assertion found in response")

    conditions = assertion.find("saml:Conditions", ns)
    if conditions is not None:
        now_utc = datetime.now(UTC)
        not_before_str = conditions.get("NotBefore", "")
        not_on_or_after_str = conditions.get("NotOnOrAfter", "")
        issue_instant_str = assertion.get("IssueInstant", "")
        if not_before_str:
            try:
                not_before = datetime.fromisoformat(not_before_str.replace("Z", "+00:00"))
                if now_utc < not_before:
                    raise ValueError("SAML assertion used before NotBefore time")
            except ValueError as exc:
                raise ValueError("Invalid SAML Conditions NotBefore format") from exc
        if not_on_or_after_str:
            try:
                not_on_or_after = datetime.fromisoformat(not_on_or_after_str.replace("Z", "+00:00"))
                if now_utc >= not_on_or_after:
                    raise ValueError("SAML assertion has expired (NotOnOrAfter)")
            except ValueError as exc:
                raise ValueError("Invalid SAML Conditions NotOnOrAfter format") from exc
        if issue_instant_str:
            try:
                issue_instant = datetime.fromisoformat(issue_instant_str.replace("Z", "+00:00"))
                if now_utc < issue_instant - timedelta(minutes=5):
                    _log.warning(
                        "sso.saml_clock_skew",
                        extra={"issue_instant": issue_instant_str, "now": now_utc.isoformat()},
                    )
            except ValueError:
                _log.warning("sso.saml_unparseable_issue_instant", extra={"issue_instant": issue_instant_str})

    subject = assertion.find(".//saml:Subject/saml:NameID", ns)
    name_id = subject.text.strip() if subject is not None and subject.text else ""

    attrs: dict[str, str] = {}
    for attr in assertion.findall(".//saml:Attribute", ns):
        attr_name = attr.get("Name", "")
        values = [v.text.strip() for v in attr.findall("saml:AttributeValue", ns) if v.text and v.text.strip()]
        if values:
            attrs[attr_name] = values[0]

    email = attrs.get("email", "") or attrs.get("Email", "") or name_id or ""
    display_name = (
        attrs.get("displayName", "")
        or attrs.get("cn", "")
        or attrs.get("firstName", "")
        or (email.split("@")[0] if "@" in email else email)
    )
    sso_subject = f"saml:{idp_entity_id}:{name_id}"

    user = await jit_provision_user(session, settings, email, display_name, "saml", sso_subject)

    saml_groups: list[str] = []
    for group_attr in ("groups", "memberOf", "Group"):
        raw = attrs.get(group_attr, "")
        if raw:
            saml_groups = [g.strip() for g in raw.split(",") if g.strip()]
            break
    if saml_groups:
        db_provider = await _lookup_provider_by_entity_id(session, idp_entity_id)
        if db_provider is not None and db_provider.group_mappings:
            await apply_group_mappings(session, user, saml_groups, db_provider.group_mappings)

    return await issue_sso_tokens(user, session, settings)


async def _saml_fetch_idp_metadata(settings: Settings) -> str:
    if settings.modulo_saml_idp_metadata_xml:
        return settings.modulo_saml_idp_metadata_xml
    if settings.modulo_saml_idp_metadata_url:
        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.modulo_saml_idp_metadata_url, timeout=15)
            resp.raise_for_status()
            return resp.text
    raise ValueError("SAML IdP metadata not configured (set MODULO_SAML_IDP_METADATA_URL or _XML)")


def _saml_parse_idp_metadata(
    xml_str: str,
) -> tuple[str, str]:
    """Parse IdP metadata XML. Returns (sso_url, entity_id)."""
    root = ET.fromstring(xml_str)
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

    return sso_url, entity_id
