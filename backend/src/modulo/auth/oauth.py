"""OAuth 2.0 authorization code flow for MCP server.

Uses authlib for RFC 6749 compliance (error handling, model mixins, scope
utilities). Token format remains JWT for stateless validation.

Supports:
- Authorization code grant (response_type=code)
- Token exchange (grant_type=authorization_code)
- Scoped access tokens (trigger:run, hitl:review, library:browse)
- Token family rotation detection (reuses pattern from jwt.py)
- Backwards-compatible API key check
"""

import base64
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from authlib.oauth2 import OAuth2Error as _OAuth2Error  # type: ignore[import-untyped]
from authlib.oauth2.rfc6749 import (  # type: ignore[import-untyped]
    ClientMixin,
    list_to_scope,
    scope_to_list,
)
from fastapi import HTTPException, status
from jwt import InvalidTokenError as JWTError
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.oauth_client import OAuthClient
from modulo.db.models.oauth_token import OAuthAuthorizationCode, OAuthConsentState, OAuthTokenFamily

_log = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_CODE_LENGTH = 64
_CODE_TTL_MINUTES = 10
_CONSENT_STATE_TTL_MINUTES = 15

VALID_SCOPES = frozenset({"trigger:run", "hitl:review", "library:browse"})


# ---------------------------------------------------------------------------
# Exceptions — extend authlib's OAuth2Error for RFC 6749 error codes
# ---------------------------------------------------------------------------


class OAuthError(_OAuth2Error):  # type: ignore[misc]
    """Base OAuth error. ``error`` maps to RFC 6749 error values."""

    def __init__(self, error_code: str, description: str = "") -> None:
        super().__init__(error=error_code, description=description)


class InvalidClientError(OAuthError):
    def __init__(self, description: str = "Invalid client credentials") -> None:
        super().__init__("invalid_client", description)


class InvalidGrantError(OAuthError):
    def __init__(self, description: str = "Invalid authorization code") -> None:
        super().__init__("invalid_grant", description)


class InvalidScopeError(OAuthError):
    def __init__(self, description: str = "Requested scope is invalid") -> None:
        super().__init__("invalid_scope", description)


class UnauthorizedClientError(OAuthError):
    def __init__(self, description: str = "Client not authorized for requested scopes") -> None:
        super().__init__("unauthorized_client", description)


# ---------------------------------------------------------------------------
# Authlib-compatible model wrappers (keep existing DB models untouched)
# ---------------------------------------------------------------------------


class AuthlibClientWrapper(ClientMixin):  # type: ignore[misc]
    """Wraps an OAuthClient ORM model for authlib ClientMixin compatibility."""

    def __init__(self, client: OAuthClient) -> None:
        self._client = client

    def get_client_id(self) -> str:
        return self._client.client_id

    def get_default_redirect_uri(self) -> str:
        uris = (self._client.redirect_uris or "").split()
        return uris[0] if uris else ""

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        allowed = (self._client.redirect_uris or "").split()
        return redirect_uri in allowed

    def check_client_secret(self, client_secret: str) -> bool:
        expected = _hash_secret(client_secret)
        return hmac.compare_digest(expected, self._client.client_secret_hash)

    def check_endpoint_auth_method(self, method: str, _endpoint: str) -> bool:
        return method == "client_secret_basic"

    def check_grant_type(self, grant_type: str) -> bool:
        return grant_type in ("authorization_code", "refresh_token")

    def check_response_type(self, response_type: str) -> bool:
        return response_type == "code"

    def get_allowed_scope(self, scope: str) -> str:
        if self._client.scopes is None:
            return ""
        allowed = set(scope_to_list(self._client.scopes))
        requested = set(scope_to_list(scope))
        return list_to_scope(sorted(allowed & requested))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_client_credentials() -> tuple[str, str, str]:
    """Return (client_id, client_secret, client_secret_hash)."""
    client_id = secrets.token_hex(8)
    client_secret = secrets.token_urlsafe(30)
    return client_id, client_secret, _hash_secret(client_secret)


async def create_oauth_client(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    scopes: str,
    redirect_uris: str,
    created_by: uuid.UUID | None = None,
) -> tuple[OAuthClient, str]:
    """Create a new OAuth client. Returns (OAuthClient, raw_client_secret)."""
    client_id, client_secret, hashed = generate_client_credentials()
    client = OAuthClient(
        organisation_id=org_id,
        client_id=client_id,
        client_secret_hash=hashed,
        name=name,
        scopes=scopes,
        redirect_uris=redirect_uris,
        account_id=created_by,
    )
    session.add(client)
    await session.flush()
    return client, client_secret


async def get_oauth_client_by_client_id(session: AsyncSession, client_id: str) -> OAuthClient | None:
    """Look up an OAuth client by its client_id. Returns None if not found."""
    result = await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    return result.scalar_one_or_none()


async def validate_client_secret(session: AsyncSession, client_id: str, client_secret: str) -> OAuthClient:
    """Validate client_id + client_secret using authlib ClientMixin. Returns the client on success."""
    client = await get_oauth_client_by_client_id(session, client_id)
    if client is None:
        raise InvalidClientError("Unknown client_id")
    wrapper = AuthlibClientWrapper(client)
    if not wrapper.check_client_secret(client_secret):
        raise InvalidClientError("Client secret mismatch")
    return client


async def list_oauth_clients(session: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """List OAuth clients for an organisation."""
    result = await session.execute(
        select(OAuthClient).where(OAuthClient.organisation_id == org_id).order_by(OAuthClient.created_at.desc())
    )
    clients = list(result.scalars())
    return [
        {
            "id": str(c.id),
            "client_id": c.client_id,
            "name": c.name,
            "scopes": c.scopes.split() if c.scopes else [],
            "redirect_uris": c.redirect_uris.split() if c.redirect_uris else [],
            "created_at": c.created_at.isoformat() if c.created_at else "",
        }
        for c in clients
    ]


async def delete_oauth_client(session: AsyncSession, client_id: str, org_id: uuid.UUID) -> bool:
    """Delete an OAuth client and cascade its auth codes and token families."""
    result = await session.execute(
        select(OAuthClient).where(
            OAuthClient.client_id == client_id,
            OAuthClient.organisation_id == org_id,
        )
    )
    client = result.scalar_one_or_none()
    if client is None:
        return False
    await session.execute(
        sa_delete(OAuthAuthorizationCode).where(
            OAuthAuthorizationCode.client_id == client_id,
            OAuthAuthorizationCode.organisation_id == org_id,
        )
    )
    await session.execute(
        sa_delete(OAuthTokenFamily).where(
            OAuthTokenFamily.client_id == client_id,
            OAuthTokenFamily.organisation_id == org_id,
        )
    )
    await session.delete(client)
    return True


# ---------------------------------------------------------------------------
# Authorization code lifecycle
# ---------------------------------------------------------------------------


def _generate_code() -> str:
    return secrets.token_urlsafe(_CODE_LENGTH)


# ---------------------------------------------------------------------------
# PKCE (RFC 7636) — S256 only
# ---------------------------------------------------------------------------


def _base64url_encode(data: bytes) -> str:
    """Base64url encode without padding (RFC 4648 §5, as used by RFC 7636)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def compute_pkce_challenge(code_verifier: str) -> str:
    """Compute the S256 code challenge for a verifier (RFC 7636 §4.2)."""
    return _base64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())


def validate_pkce_method(code_challenge_method: str | None) -> str:
    """Validate the PKCE method — S256 is mandatory, plain/empty rejected.

    Returns the normalized method ("S256"). Raises InvalidGrantError on any
    non-S256 value so the challenge is always verifiable at exchange time.
    """
    if code_challenge_method is None or not code_challenge_method.strip():
        raise InvalidGrantError("PKCE code_challenge_method 'S256' is required")
    normalized = code_challenge_method.strip().upper()
    if normalized != "S256":
        raise InvalidGrantError("PKCE code_challenge_method must be 'S256'")
    return normalized


def verify_pkce(
    code_verifier: str | None,
    code_challenge: str | None,
    code_challenge_method: str | None,
) -> None:
    """Verify a PKCE code_verifier against the stored challenge (RFC 7636 §4.6).

    Fail-closed: missing verifier, missing challenge, or a non-S256 method all
    raise InvalidGrantError. Comparison is constant-time via hmac.compare_digest.
    """
    if not code_verifier or not code_verifier.strip():
        raise InvalidGrantError("PKCE code_verifier is required")
    validate_pkce_method(code_challenge_method)
    if not code_challenge or not code_challenge.strip():
        raise InvalidGrantError("PKCE code_challenge missing from authorization code")
    expected = compute_pkce_challenge(code_verifier)
    if not hmac.compare_digest(expected.encode("ascii"), code_challenge.encode("ascii")):
        raise InvalidGrantError("PKCE verification failed - code_verifier does not match code_challenge")


async def create_authorization_code(
    session: AsyncSession,
    *,
    client_id: str,
    org_id: uuid.UUID,
    scopes: str,
    redirect_uri: str,
    account_id: uuid.UUID,
    code_challenge: str,
    code_challenge_method: str = "S256",
) -> str:
    """Generate and store a one-time, account-bound authorization code.

    The code is minted ONLY by the authenticated consent approve endpoint
    (ADR 017 DECISION 1 — approve POST is the consent). ``account_id`` is the
    account that approved; ``code_challenge`` comes from the consent state row
    (never client-supplied at approve). The challenge is verified at token
    exchange via ``verify_pkce``.
    """
    normalized_method = validate_pkce_method(code_challenge_method)
    code = _generate_code()
    auth_code = OAuthAuthorizationCode(
        code=code,
        client_id=client_id,
        organisation_id=org_id,
        account_id=account_id,
        scopes=scopes,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=normalized_method,
        expires_at=datetime.now(UTC) + timedelta(minutes=_CODE_TTL_MINUTES),
    )
    session.add(auth_code)
    await session.flush()
    return code


async def consume_authorization_code(
    session: AsyncSession,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    client_secret: str,
    code_verifier: str | None = None,
) -> OAuthAuthorizationCode:
    """Validate and consume a one-time authorization code.

    Validates client credentials, code properties, and the PKCE code_verifier
    against the stored S256 challenge, then marks the code used. Uses authlib's
    AuthlibClientWrapper for credential validation and the authlib exception
    hierarchy for RFC-compliant error codes.
    """
    client = await validate_client_secret(session, client_id, client_secret)

    wrapper = AuthlibClientWrapper(client)
    if not wrapper.check_redirect_uri(redirect_uri):
        raise InvalidGrantError("redirect_uri mismatch")

    try:
        async with session.begin():
            result = await session.execute(
                select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code).with_for_update()
            )
            auth_code = result.scalar_one_or_none()
            if auth_code is None:
                raise InvalidGrantError("Authorization code not found")

            if auth_code.client_id != client_id:
                raise InvalidGrantError("Authorization code was issued to a different client")

            if auth_code.redirect_uri != redirect_uri:
                raise InvalidGrantError("redirect_uri mismatch")

            if auth_code.used:
                raise InvalidGrantError("Authorization code has already been used")

            if auth_code.expires_at < datetime.now(UTC):
                raise InvalidGrantError("Authorization code has expired")

            # PKCE must be verified BEFORE the code is consumed — a failing
            # verifier leaves the code intact for a legitimate retry.
            verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method)

            auth_code.used = True
            await session.flush()
    except ProgrammingError:
        _log.exception("auth.oauth")

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None

    return auth_code


# ---------------------------------------------------------------------------
# Consent-state store (ADR 017 A1b)
# ---------------------------------------------------------------------------


async def create_consent_state(
    session: AsyncSession,
    *,
    state: str,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    code_challenge: str,
    org_id: uuid.UUID,
) -> None:
    """Persist a browser consent handoff created by the anonymous authorize 302.

    ``account_id`` stays NULL until the authenticated approve POST populates
    it. The state is single-use and TTL-bounded (~15 min). The stored scopes
    and code_challenge are the ONLY source at mint time — a tampered approve
    payload can never escalate them (approve re-reads the state row).
    """
    state_row = OAuthConsentState(
        state=state,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        code_challenge=code_challenge,
        organisation_id=org_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=_CONSENT_STATE_TTL_MINUTES),
    )
    session.add(state_row)
    await session.flush()


async def consume_consent_state(
    session: AsyncSession, *, state: str, _org_id: uuid.UUID, account_id: uuid.UUID
) -> OAuthConsentState | None:
    """Atomically claim and return an unexpired, unconsumed consent state.

    Uses ``UPDATE ... WHERE state=:s AND consumed=false AND expires_at > now
    RETURNING`` so two concurrent approves cannot both consume the same state
    (TOCTOU-safe). ``account_id`` (the Bearer principal who approved) is
    stamped onto the row for auditability. Returns None for unknown,
    already-consumed, or expired states — the caller denies. RLS context is set
    by the caller before this runs, so the UPDATE is also org-bounded at the
    DB layer (cross-org states are never visible and return None).
    """
    try:
        result = await session.execute(
            update(OAuthConsentState)
            .where(
                OAuthConsentState.state == state,
                OAuthConsentState.consumed.is_(False),
                OAuthConsentState.expires_at > datetime.now(UTC),
            )
            .values(consumed=True, account_id=account_id)
            .returning(OAuthConsentState)
        )
    except ProgrammingError:
        _log.exception("auth.oauth")

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Access token creation & validation
# ---------------------------------------------------------------------------

_OAUTH_ACCESS_TOKEN_MINUTES = 60


@dataclass(frozen=True)
class OAuthAccessTokenClaims:
    """Decoded claims from an OAuth access token JWT."""

    client_id: str
    organisation_id: uuid.UUID
    account_id: uuid.UUID
    scopes: list[str]
    token_family: str
    token_sequence: int


def create_oauth_access_token(
    client_id: str,
    secret_key: str,
    *,
    organisation_id: str,
    account_id: str,
    scopes: list[str],
    token_family: str,
    token_sequence: int,
) -> str:
    """Issue a JWT access token for OAuth client credentials flow.

    Carries the consenting account's ``account_id`` so the MCP middleware can
    resolve the account's LIVE org role per call and clamp scope-derived roles
    to it (ADR 017) instead of synthesising a uuid5(client_id) actor.
    """
    now = datetime.now(UTC)
    claims = {
        "sub": client_id,
        "org_id": organisation_id,
        "account_id": account_id,
        "scopes": " ".join(scopes),
        "purpose": "oauth_access",
        "token_family": token_family,
        "token_sequence": token_sequence,
        "iat": now,
        "exp": now + timedelta(minutes=_OAUTH_ACCESS_TOKEN_MINUTES),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def decode_oauth_access_token(token: str, secret_key: str) -> OAuthAccessTokenClaims:
    """Decode and validate an OAuth access token JWT.

    Returns parsed claims on success. Raises JWTError on any failure.
    """
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    purpose = payload.get("purpose")
    if purpose != "oauth_access":
        raise JWTError(f"Token purpose '{purpose}' is not 'oauth_access'")

    client_id = payload.get("sub")
    if not isinstance(client_id, str) or not client_id:
        raise JWTError("Token missing or invalid 'sub' claim")

    org_id_str = payload.get("org_id")
    if not isinstance(org_id_str, str):
        raise JWTError("Token missing or invalid 'org_id' claim")

    account_id_str = payload.get("account_id")
    if not isinstance(account_id_str, str) or not account_id_str:
        raise JWTError("Token missing or invalid 'account_id' claim")

    scopes_str = payload.get("scopes")
    if not isinstance(scopes_str, str):
        scopes_str = ""

    token_family = payload.get("token_family")
    if not isinstance(token_family, str) or not token_family:
        raise JWTError("Token missing 'token_family'")

    token_sequence = payload.get("token_sequence")
    if not isinstance(token_sequence, int):
        raise JWTError("Token missing 'token_sequence'")

    try:
        parsed_org_id = uuid.UUID(org_id_str)
        parsed_account_id = uuid.UUID(account_id_str)
    except ValueError as exc:
        raise JWTError("Token contains malformed org_id/account_id") from exc

    return OAuthAccessTokenClaims(
        client_id=client_id,
        organisation_id=parsed_org_id,
        account_id=parsed_account_id,
        scopes=scopes_str.split(),
        token_family=token_family,
        token_sequence=token_sequence,
    )


# ---------------------------------------------------------------------------
# Token family management (rotation detection)
# ---------------------------------------------------------------------------


async def _get_token_family(
    session: AsyncSession, family_id: str, client_id: str, org_id: uuid.UUID
) -> OAuthTokenFamily | None:
    """Look up a token family by ID, client, and org."""
    try:
        fid = uuid.UUID(family_id)
    except ValueError:
        raise InvalidGrantError(f"Invalid token family ID: '{family_id}'") from None
    result = await session.execute(
        select(OAuthTokenFamily)
        .where(
            OAuthTokenFamily.family_id == fid,
            OAuthTokenFamily.client_id == client_id,
            OAuthTokenFamily.organisation_id == org_id,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def create_oauth_token_family(
    session: AsyncSession,
    *,
    client_id: str,
    org_id: uuid.UUID,
) -> tuple[str, int]:
    """Create a new token family. Returns (family_id, sequence=0)."""
    family = OAuthTokenFamily(
        client_id=client_id,
        organisation_id=org_id,
        max_sequence=0,
    )
    session.add(family)
    await session.flush()
    return str(family.family_id), 0


async def rotate_oauth_token_family(
    session: AsyncSession,
    *,
    family_id: str,
    current_sequence: int,
    client_id: str,
    org_id: uuid.UUID,
) -> tuple[str, int]:
    """Increment token sequence. Returns (family_id, new_sequence).

    If ``current_sequence`` does not match the stored ``max_sequence``, the
    family is blacklisted (token theft detected) and an InvalidGrantError
    is raised.
    """
    family = await _get_token_family(session, family_id, client_id, org_id)
    if family is None:
        raise InvalidGrantError("Token family not found")

    if family.is_blacklisted:
        raise InvalidGrantError("Token family has been blacklisted")

    if family.max_sequence != current_sequence:
        family.is_blacklisted = True
        family.blacklisted_at = datetime.now(UTC)
        await session.flush()
        _log.warning(
            "oauth.token_theft_detected",
            extra={
                "family_id": str(family_id),
                "client_id": client_id,
                "expected_sequence": family.max_sequence,
                "current_sequence": current_sequence,
            },
        )
        raise InvalidGrantError(
            "Token family rotated out of order - possible token theft. This family has been blacklisted."
        )

    new_sequence = family.max_sequence + 1
    family.max_sequence = new_sequence
    await session.flush()
    return str(family.family_id), new_sequence


async def blacklist_oauth_token_family(
    session: AsyncSession,
    *,
    family_id: str,
    client_id: str,
    org_id: uuid.UUID,
) -> None:
    """Explicitly invalidate a token family (logout equivalent)."""
    family = await _get_token_family(session, family_id, client_id, org_id)
    if family is not None and not family.is_blacklisted:
        family.is_blacklisted = True
        family.blacklisted_at = datetime.now(UTC)
        await session.flush()


async def check_oauth_token_family_valid(
    session: AsyncSession,
    *,
    family_id: str,
    client_id: str,
    org_id: uuid.UUID,
) -> bool:
    """Check whether a token family is still valid (not blacklisted)."""
    family = await _get_token_family(session, family_id, client_id, org_id)
    return family is not None and not family.is_blacklisted


# ---------------------------------------------------------------------------
# Refresh token creation & validation (OAuth-specific, not user-level JWT)
# ---------------------------------------------------------------------------

_OAUTH_REFRESH_TOKEN_DAYS = 30


@dataclass(frozen=True)
class OAuthRefreshTokenClaims:
    """Decoded claims from an OAuth refresh token JWT."""

    client_id: str
    organisation_id: uuid.UUID
    account_id: uuid.UUID
    scopes: list[str]
    token_family: str
    token_sequence: int


def create_oauth_refresh_token(
    client_id: str,
    secret_key: str,
    *,
    organisation_id: str,
    account_id: str,
    scopes: list[str],
    token_family: str,
    token_sequence: int,
    expires_delta: timedelta = timedelta(days=_OAUTH_REFRESH_TOKEN_DAYS),
) -> str:
    """Issue a JWT refresh token for OAuth client credentials flow."""
    now = datetime.now(UTC)
    claims = {
        "purpose": "oauth_refresh",
        "sub": client_id,
        "org_id": organisation_id,
        "account_id": account_id,
        "scopes": " ".join(scopes),
        "token_family": token_family,
        "token_sequence": token_sequence,
        "iat": now,
        "exp": now + expires_delta,
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def decode_oauth_refresh_token(token: str, secret_key: str) -> OAuthRefreshTokenClaims:
    """Decode and validate an OAuth refresh token JWT.

    Returns parsed claims on success. Raises JWTError on any failure.
    """
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    purpose = payload.get("purpose")
    if purpose != "oauth_refresh":
        raise JWTError(f"Token purpose '{purpose}' is not 'oauth_refresh'")

    client_id = payload.get("sub")
    if not isinstance(client_id, str) or not client_id:
        raise JWTError("Token missing or invalid 'sub' claim")

    org_id_str = payload.get("org_id")
    if not isinstance(org_id_str, str):
        raise JWTError("Token missing or invalid 'org_id' claim")

    account_id_str = payload.get("account_id")
    if not isinstance(account_id_str, str) or not account_id_str:
        raise JWTError("Token missing or invalid 'account_id' claim")

    scopes_str = payload.get("scopes")
    if not isinstance(scopes_str, str):
        scopes_str = ""

    token_family = payload.get("token_family")
    if not isinstance(token_family, str) or not token_family:
        raise JWTError("Token missing 'token_family'")

    token_sequence = payload.get("token_sequence")
    if not isinstance(token_sequence, int):
        raise JWTError("Token missing 'token_sequence'")

    try:
        parsed_org_id = uuid.UUID(org_id_str)
        parsed_account_id = uuid.UUID(account_id_str)
    except ValueError as exc:
        raise JWTError("Token contains malformed org_id/account_id") from exc

    return OAuthRefreshTokenClaims(
        client_id=client_id,
        organisation_id=parsed_org_id,
        account_id=parsed_account_id,
        scopes=scopes_str.split(),
        token_family=token_family,
        token_sequence=token_sequence,
    )


# ---------------------------------------------------------------------------
# Scope helpers (using authlib's scope_to_list / list_to_scope)
# ---------------------------------------------------------------------------


def normalize_scopes(requested: str) -> list[str]:
    """Parse and validate a space-separated scope string.

    Returns the sorted list of valid scopes. Raises InvalidScopeError if
    any requested scope is not in VALID_SCOPES.
    """
    if not requested or not requested.strip():
        return []
    parts = scope_to_list(requested)
    for s in parts:
        if s not in VALID_SCOPES:
            raise InvalidScopeError(f"Unknown scope: '{s}'")
    return sorted(parts)


def validate_client_scopes(client: OAuthClient, requested_scopes: list[str]) -> list[str]:
    """Intersect requested scopes with the client's allowed scopes.

    Uses authlib's ClientMixin-compatible wrapper for scope intersection.
    Raises UnauthorizedClientError if no scopes remain after intersection.
    """
    wrapper = AuthlibClientWrapper(client)
    allowed_scope = wrapper.get_allowed_scope(list_to_scope(requested_scopes))
    valid = scope_to_list(allowed_scope)
    if not valid:
        raise UnauthorizedClientError("None of the requested scopes are allowed for this client")
    return valid  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Scope → role mapping (shared by token/refresh issuance and MCP middleware)
# ---------------------------------------------------------------------------


def scopes_required_role(scopes: list[str]) -> str:
    """Return the minimum org role a live account must hold to carry these scopes.

    Mirrors the MCP middleware's scope→role ladder: ``hitl:review`` requires
    ``operator``; anything else is ``runner`` (ADR 017 — scope grants can never
    exceed the account's live role).
    """
    if "hitl:review" in scopes:
        return "operator"
    return "runner"


def clamp_oauth_role(scope_role: str, live_role: str) -> str:
    """Clamp a scope-derived role to the account's live org role (the lower wins).

    A token's scope grants can never exceed what the account currently holds:
    a demoted operator's ``hitl:review`` token degrades to the live role on the
    next call (ADR 017 per-call live re-validation). Pure + unit-testable, no DB.
    """
    from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

    if scope_role not in ORG_ROLE_HIERARCHY or live_role not in ORG_ROLE_HIERARCHY:
        return live_role
    if ORG_ROLE_HIERARCHY[scope_role] <= ORG_ROLE_HIERARCHY[live_role]:
        return scope_role
    return live_role


async def verify_live_role_covers_scopes(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
    scopes: list[str],
) -> str:
    """Resolve the account's LIVE org role and assert it covers the granted scopes.

    Returns the live role on success. Raises InvalidGrantError when the account
    has no active membership or its live role is below what the scopes require —
    the token/refresh endpoints then deny issuance (fail-closed, ADR 017).
    """
    from modulo.auth.dependencies import resolve_role_from_membership
    from modulo.auth.permissions import PermissionDenied, assert_org_role

    live_role = await resolve_role_from_membership(session, str(account_id), str(org_id))
    if live_role is None:
        raise InvalidGrantError("Account has no active membership for this organisation")
    required = scopes_required_role(scopes)
    try:
        assert_org_role(live_role, required, subject="OAuth scope grant")
    except PermissionDenied as exc:
        raise InvalidGrantError(f"Account role does not cover the granted scopes: {exc}") from exc
    return live_role
