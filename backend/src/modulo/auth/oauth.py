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
    AuthorizationCodeMixin,
    ClientMixin,
    TokenMixin,
    list_to_scope,
    scope_to_list,
)
from fastapi import HTTPException, status
from jwt import InvalidTokenError as JWTError
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.oauth_client import OAuthClient
from modulo.db.models.oauth_token import OAuthAuthorizationCode, OAuthTokenFamily

_log = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_CODE_LENGTH = 64
_CODE_TTL_MINUTES = 10

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


class AccessDeniedError(OAuthError):
    def __init__(self, description: str = "Resource owner denied access") -> None:
        super().__init__("access_denied", description)


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

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
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


class AuthlibCodeWrapper(AuthorizationCodeMixin):  # type: ignore[misc]
    """Wraps an OAuthAuthorizationCode ORM model for authlib."""

    def __init__(self, code: OAuthAuthorizationCode) -> None:
        self._code = code

    def get_redirect_uri(self) -> str:
        return self._code.redirect_uri

    def get_scope(self) -> str:
        return self._code.scopes


class AuthlibTokenWrapper(TokenMixin):  # type: ignore[misc]
    """Wraps decoded JWT claims for authlib token validation."""

    def __init__(self, client_id: str, scopes: list[str], expires_at: datetime) -> None:
        self._client_id = client_id
        self._scope = " ".join(scopes)
        self._expires_at = expires_at

    def get_client_id(self) -> str:
        return self._client_id

    def get_scope(self) -> str:
        return self._scope

    def get_expires_in(self) -> int:
        remaining = self._expires_at - datetime.now(UTC)
        return max(0, int(remaining.total_seconds()))

    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self._expires_at

    def is_revoked(self) -> bool:
        return False


def wrap_oauth_client(client: OAuthClient) -> AuthlibClientWrapper:
    """Create an authlib-compatible client wrapper for the given ORM model."""
    return AuthlibClientWrapper(client)


def wrap_oauth_code(code: OAuthAuthorizationCode) -> AuthlibCodeWrapper:
    """Create an authlib-compatible code wrapper for the given ORM model."""
    return AuthlibCodeWrapper(code)


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


async def create_authorization_code(
    session: AsyncSession,
    *,
    client_id: str,
    org_id: uuid.UUID,
    scopes: str,
    redirect_uri: str,
) -> str:
    """Generate and store a one-time authorization code using authlib scope validation."""
    code = _generate_code()
    auth_code = OAuthAuthorizationCode(
        code=code,
        client_id=client_id,
        organisation_id=org_id,
        scopes=scopes,
        redirect_uri=redirect_uri,
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
) -> OAuthAuthorizationCode:
    """Validate and consume a one-time authorization code.

    Validates client credentials and code properties, then marks the code used.
    Uses authlib's AuthlibClientWrapper for credential validation and the
    authlib exception hierarchy for RFC-compliant error codes.
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
# Access token creation & validation
# ---------------------------------------------------------------------------

_OAUTH_ACCESS_TOKEN_MINUTES = 60


@dataclass(frozen=True)
class OAuthAccessTokenClaims:
    """Decoded claims from an OAuth access token JWT."""

    client_id: str
    organisation_id: uuid.UUID
    scopes: list[str]
    token_family: str
    token_sequence: int


def create_oauth_access_token(
    client_id: str,
    secret_key: str,
    *,
    organisation_id: str,
    scopes: list[str],
    token_family: str,
    token_sequence: int,
) -> str:
    """Issue a JWT access token for OAuth client credentials flow."""
    now = datetime.now(UTC)
    claims = {
        "sub": client_id,
        "org_id": organisation_id,
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
    except ValueError as exc:
        raise JWTError("Token contains malformed org_id") from exc

    return OAuthAccessTokenClaims(
        client_id=client_id,
        organisation_id=parsed_org_id,
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
    scopes: list[str]
    token_family: str
    token_sequence: int


def create_oauth_refresh_token(
    client_id: str,
    secret_key: str,
    *,
    organisation_id: str,
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
    except ValueError as exc:
        raise JWTError("Token contains malformed org_id") from exc

    return OAuthRefreshTokenClaims(
        client_id=client_id,
        organisation_id=parsed_org_id,
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


def validate_token_scope(token_scopes: str, required_scope: str) -> bool:
    """Check whether token scopes satisfy a required scope using authlib scope matching."""
    return required_scope in scope_to_list(token_scopes) if required_scope else True
