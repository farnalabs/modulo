"""OIDC ID token signature verification via JWKS.

Fetches the JWKS from the provider's ``jwks_uri`` (discovered via the
OpenID Connect discovery document), caches it in memory with a 1-hour TTL,
and verifies the ``id_token`` JWT signature using the matching JWK.

Validation performed:
- ``iss`` matches the provider's issuer
- ``aud`` matches the client_id
- ``exp`` is not expired
- JWT signature matches the key from the JWKS
- ``alg`` is restricted to an allowlist (``none`` is rejected)
"""

import base64
import json
import logging
import time
from typing import Any

import httpx
import jwt
from jwt import InvalidTokenError as JWTError
from jwt import PyJWK, PyJWKError

_log = logging.getLogger(__name__)

_JWKS_CACHE_TTL = 3600  # 1 hour
_jwks_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_ACCEPTABLE_JWT_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "ES256",
        "ES384",
        "ES512",
        "PS256",
        "PS384",
        "PS512",
        "EdDSA",
    }
)


class OidcVerifyError(Exception):
    """Raised when OIDC ID token verification fails."""


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def clear_jwks_cache() -> None:
    """Clear the JWKS cache (for testing or manual invalidation)."""
    _jwks_cache.clear()


def _cache_get(jwks_uri: str) -> list[dict[str, Any]] | None:
    entry = _jwks_cache.get(jwks_uri)
    if entry is None:
        return None
    fetched_at, keys = entry
    if time.time() - fetched_at >= _JWKS_CACHE_TTL:
        _jwks_cache.pop(jwks_uri, None)
        return None
    return keys


def _cache_set(jwks_uri: str, keys: list[dict[str, Any]]) -> None:
    _jwks_cache[jwks_uri] = (time.time(), keys)


# ---------------------------------------------------------------------------
# JWKS fetching
# ---------------------------------------------------------------------------


async def _fetch_discovery_document(discovery_url: str) -> dict[str, Any]:
    """Fetch and validate the provider's OpenID discovery document."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(discovery_url, timeout=10)
            resp.raise_for_status()
            disc: dict[str, Any] = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as exc:
        raise OidcVerifyError(f"Failed to fetch discovery document: {exc}") from exc
    if not isinstance(disc, dict):
        raise OidcVerifyError("Discovery document is not a JSON object")
    return disc


async def _fetch_jwks(jwks_uri: str) -> list[dict[str, Any]]:
    """Fetch the JWKS, using a cached copy within the TTL window."""
    cached = _cache_get(jwks_uri)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(jwks_uri, timeout=10)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as exc:
        raise OidcVerifyError(f"Failed to fetch JWKS: {exc}") from exc

    if not isinstance(data, dict):
        raise OidcVerifyError("JWKS response is not a JSON object")
    raw_keys = data.get("keys", [])
    if not isinstance(raw_keys, list) or not raw_keys:
        raise OidcVerifyError("No keys in JWKS response")

    _cache_set(jwks_uri, raw_keys)
    return raw_keys


async def _fetch_jwks_force(jwks_uri: str) -> list[dict[str, Any]]:
    """Bypass cache and fetch JWKS fresh (used on rotation retry)."""
    _jwks_cache.pop(jwks_uri, None)
    return await _fetch_jwks(jwks_uri)


# ---------------------------------------------------------------------------
# JWK selection
# ---------------------------------------------------------------------------


def _find_jwk(jwks: list[dict[str, Any]], kid: str | None) -> dict[str, Any]:
    """Find the JWK matching the given ``kid``.

    Falls back to the first key in the set if no ``kid`` is provided
    (some providers omit ``kid`` from the JWT header).
    """
    if kid:
        for key in jwks:
            if key.get("kid") == kid:
                return key

        # No match found — callers should retry with a fresh JWKS fetch
        raise OidcVerifyError(f"No JWK found with kid '{kid}'")

    return jwks[0]


# ---------------------------------------------------------------------------
# ID token header decoding
# ---------------------------------------------------------------------------


def _decode_jwt_header(token: str) -> dict[str, Any]:
    """Decode the JWT header without signature verification."""
    parts = token.split(".")
    if len(parts) != 3:
        raise OidcVerifyError("Invalid JWT format: expected 3 dot-separated segments")
    try:
        padded = parts[0] + "=" * (-len(parts[0]) % 4)
        return dict(json.loads(base64.urlsafe_b64decode(padded)))
    except ValueError as exc:
        raise OidcVerifyError(f"Failed to decode JWT header: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def verify_id_token(
    id_token: str,
    jwks_uri: str,
    client_id: str,
    issuer: str,
) -> dict[str, Any]:
    """Verify an OIDC ID token using the provider's JWKS.

    Args:
        id_token: The raw JWT string returned by the token endpoint.
        jwks_uri: The provider's JWKS URI (from the discovery document).
        client_id: The OAuth 2.0 client ID (validates the ``aud`` claim).
        issuer: The expected issuer URL (validates the ``iss`` claim).

    Returns:
        The verified claims dict on success.

    Raises:
        OidcVerifyError: If signature verification or claim validation fails.

    """
    header = _decode_jwt_header(id_token)
    kid = header.get("kid")
    alg = header.get("alg", "RS256")
    if alg not in _ACCEPTABLE_JWT_ALGORITHMS:
        raise OidcVerifyError(f"Unsupported JWT algorithm '{alg}' — rejected")

    jwks = await _fetch_jwks(jwks_uri)

    try:
        jwk_dict = _find_jwk(jwks, kid)
    except OidcVerifyError:
        if kid:
            jwks = await _fetch_jwks_force(jwks_uri)
            jwk_dict = _find_jwk(jwks, kid)
        else:
            raise

    return await _decode_and_verify(id_token, jwk_dict, alg, client_id, issuer, jwks_uri, header_kid=kid)


async def verify_id_token_with_discovery(
    id_token: str,
    discovery_url: str,
    client_id: str,
) -> dict[str, Any]:
    """Verify an OIDC ID token using the provider's discovery URL.

    Fetches the discovery document internally to obtain the JWKS URI and
    issuer. Prefer :func:`verify_id_token` when you already have the JWKS
    URI and issuer from a previously fetched discovery document.
    """
    disc = await _fetch_discovery_document(discovery_url)
    jwks_uri = disc.get("jwks_uri")
    if not jwks_uri:
        raise OidcVerifyError("No jwks_uri in discovery document")

    issuer = disc.get("issuer", "")
    if not issuer:
        raise OidcVerifyError("No issuer in discovery document")

    return await verify_id_token(id_token, jwks_uri, client_id, issuer)


async def _decode_and_verify(
    id_token: str,
    jwk_dict: dict[str, Any],
    alg: str,
    client_id: str,
    issuer: str,
    jwks_uri: str,
    header_kid: str | None = None,
) -> dict[str, Any]:
    """Construct the key from JWK and verify the JWT."""
    try:
        key = PyJWK.from_dict(jwk_dict).key
    except PyJWKError as exc:
        raise OidcVerifyError(f"Failed to construct key from JWK: {exc}") from exc

    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[alg],
            audience=client_id,
            issuer=issuer,
        )
    except JWTError as exc:
        _log.info("oidc_verify.retry_on_failure", extra={"jwks_uri": jwks_uri})
        _jwks_cache.pop(jwks_uri, None)
        try:
            jwks = await _fetch_jwks_force(jwks_uri)
            jwk_dict = _find_jwk(jwks, header_kid)
            key = PyJWK.from_dict(jwk_dict).key
            claims = jwt.decode(
                id_token,
                key,
                algorithms=[alg],
                audience=client_id,
                issuer=issuer,
            )
        except (JWTError, PyJWKError, OidcVerifyError) as exc2:
            raise OidcVerifyError(f"ID token verification failed after retry: {exc2}") from exc

    return dict(claims)
