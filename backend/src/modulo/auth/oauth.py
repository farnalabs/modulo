"""OAuth 2.0 authorization code flow for MCP server.

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

from jose import JWTError, jwt
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.oauth_client import OAuthClient
from modulo.db.models.oauth_token import OAuthAuthorizationCode, OAuthTokenFamily

_log = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_CODE_LENGTH = 64
_CODE_TTL_MINUTES = 10
_ACCESS_TOKEN_MINUTES = 60

VALID_SCOPES = frozenset({"trigger:run", "hitl:review", "library:browse"})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OAuthError(Exception):
    """Base OAuth error. The ``error_code`` maps to RFC 6749 error values."""

    def __init__(self, error_code: str, description: str = "") -> None:
        self.error_code = error_code
        self.description = description
        super().__init__(f"{error_code}: {description}")


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
# Client management
# ---------------------------------------------------------------------------


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_client_credentials() -> tuple[str, str, str]:
    """Return (client_id, client_secret, client_secret_hash).

    client_id:    16-char hex prefix
    client_secret: 40-char url-safe token
    """
    client_id = secrets.token_hex(8)  # 16 hex chars
    client_secret = secrets.token_urlsafe(30)  # 40 chars
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
    """Create a new OAuth client. Returns (OAuthClient, raw_client_secret).

    The raw client_secret is shown once to the caller and never stored.
    """
    client_id, client_secret, hashed = generate_client_credentials()
    client = OAuthClient(
        organisation_id=org_id,
        client_id=client_id,
        client_secret_hash=hashed,
        name=name,
        scopes=scopes,
        redirect_uris=redirect_uris,
        created_by=created_by,
    )
    session.add(client)
    await session.flush()
    return client, client_secret


async def get_oauth_client_by_client_id(session: AsyncSession, client_id: str) -> OAuthClient | None:
    result = await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    return result.scalar_one_or_none()


async def validate_client_secret(session: AsyncSession, client_id: str, client_secret: str) -> OAuthClient:
    """Validate client_id + client_secret. Returns the client on success."""
    client = await get_oauth_client_by_client_id(session, client_id)
    if client is None:
        raise InvalidClientError("Unknown client_id")
    expected = client.client_secret_hash
    actual = _hash_secret(client_secret)
    if not hmac.compare_digest(expected, actual):
        raise InvalidClientError("Client secret mismatch")
    return client


async def list_oauth_clients(session: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        select(OAuthClient).where(OAuthClient.organisation_id == org_id).order_by(OAuthClient.created_at.desc())
    )
    clients = list(result.scalars())
    return [
        {
            "id": str(c.id),
            "client_id": c.client_id,
            "name": c.name,
            "scopes": c.scopes.split(),
            "redirect_uris": c.redirect_uris.split(),
            "created_at": c.created_at.isoformat(),
        }
        for c in clients
    ]


async def delete_oauth_client(session: AsyncSession, client_id: str, org_id: uuid.UUID) -> bool:
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
    """Generate and store a one-time authorization code. Returns the raw code."""
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

    Validates:
    - Client credentials
    - Code exists and is not expired
    - Code belongs to this client
    - redirect_uri matches
    - Code not already used (single-use)

    Returns the consumed code on success.
    """
    await validate_client_secret(session, client_id, client_secret)

    result = await session.execute(select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code))
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

    If `current_sequence` does not match the stored `max_sequence`, the
    family is blacklisted (token theft detected) and an InvalidGrantError
    is raised.
    """
    fid = uuid.UUID(family_id)
    result = await session.execute(
        select(OAuthTokenFamily).where(
            OAuthTokenFamily.family_id == fid,
            OAuthTokenFamily.client_id == client_id,
            OAuthTokenFamily.organisation_id == org_id,
        )
    )
    family = result.scalar_one_or_none()
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
            "Token family rotated out of order — possible token theft. This family has been blacklisted."
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
    fid = uuid.UUID(family_id)
    result = await session.execute(
        select(OAuthTokenFamily).where(
            OAuthTokenFamily.family_id == fid,
            OAuthTokenFamily.client_id == client_id,
            OAuthTokenFamily.organisation_id == org_id,
        )
    )
    family = result.scalar_one_or_none()
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
    fid = uuid.UUID(family_id)
    result = await session.execute(
        select(OAuthTokenFamily).where(
            OAuthTokenFamily.family_id == fid,
            OAuthTokenFamily.client_id == client_id,
            OAuthTokenFamily.organisation_id == org_id,
            OAuthTokenFamily.is_blacklisted.is_(False),
        )
    )
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def normalize_scopes(requested: str) -> list[str]:
    """Parse and validate a space-separated scope string.

    Returns the sorted list of valid scopes. Raises InvalidScopeError if
    any requested scope is not in VALID_SCOPES.
    """
    if not requested.strip():
        return []
    parts = requested.strip().split()
    for s in parts:
        if s not in VALID_SCOPES:
            raise InvalidScopeError(f"Unknown scope: '{s}'")
    return sorted(parts)


def validate_client_scopes(client: OAuthClient, requested_scopes: list[str]) -> list[str]:
    """Intersect requested scopes with the client's allowed scopes.

    Raises UnauthorizedClientError if no scopes remain after intersection.
    """
    allowed = set(client.scopes.split())
    requested = set(requested_scopes)
    valid = sorted(allowed & requested)
    if not valid:
        raise UnauthorizedClientError("None of the requested scopes are allowed for this client")
    return valid
