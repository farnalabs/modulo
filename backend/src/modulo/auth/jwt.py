"""JWT utilities for Modulo v1 user management.

Always uses HS256. The `none` algorithm is excluded from decode's allowed list,
so tokens signed with `alg: none` are rejected by python-jose before we see them.

Token families: Each refresh token belongs to a family. On refresh, the sequence
number is incremented. If a stale sequence is presented (token theft), the entire
family is blacklisted. On logout, the family is explicitly invalidated.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

_ALGORITHM = "HS256"
_ACCESS_TOKEN_MINUTES = 60
_REFRESH_TOKEN_HOURS = 24
_WS_TOKEN_MINUTES = 15


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Identity and tenant claims from a verified access token."""

    username: str
    organisation_id: uuid.UUID
    user_id: uuid.UUID
    org_role: str


def create_access_token(
    subject: str,
    secret_key: str,
    *,
    organisation_id: str,
    user_id: str,
    org_role: str,
) -> str:
    """1-hour access token."""
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "org_id": organisation_id,
        "user_id": user_id,
        "org_role": org_role,
        "iat": now,
        "exp": now + timedelta(minutes=_ACCESS_TOKEN_MINUTES),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def create_refresh_token(
    subject: str,
    secret_key: str,
    *,
    organisation_id: str,
    user_id: str,
    org_role: str,
    token_family: str,
    token_sequence: int,
) -> str:
    """24-hour refresh token with family+sequence for rotation detection."""
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "org_id": organisation_id,
        "user_id": user_id,
        "org_role": org_role,
        "purpose": "refresh",
        "token_family": token_family,
        "token_sequence": token_sequence,
        "iat": now,
        "exp": now + timedelta(hours=_REFRESH_TOKEN_HOURS),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def refresh_access_token(refresh_token: str, secret_key: str) -> str:
    """Validate a refresh token and issue a new 1-hour access token."""
    principal = decode_principal(refresh_token, secret_key, allowed_purposes=["refresh"])
    return create_access_token(
        principal.username,
        secret_key,
        organisation_id=str(principal.organisation_id),
        user_id=str(principal.user_id),
        org_role=principal.org_role,
    )


def decode_principal(
    token: str, secret_key: str, allowed_purposes: list[str] | None = None
) -> AuthenticatedPrincipal:
    """Decode and validate all identity claims needed for tenant-scoped API access."""
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    sub = payload.get("sub")
    org_id = payload.get("org_id")
    user_id = payload.get("user_id")
    org_role = payload.get("org_role")
    if not isinstance(sub, str) or not sub:
        raise JWTError("Token missing or invalid 'sub' claim")
    if not isinstance(org_id, str):
        raise JWTError("Token missing or invalid 'org_id' claim")
    if not isinstance(user_id, str):
        raise JWTError("Token missing or invalid 'user_id' claim")
    if not isinstance(org_role, str) or not org_role:
        raise JWTError("Token missing or invalid 'org_role' claim")
    if allowed_purposes is not None:
        purpose = payload.get("purpose")
        if not isinstance(purpose, str) or purpose not in allowed_purposes:
            raise JWTError(f"Token purpose '{purpose}' not in allowed list: {allowed_purposes}")
    try:
        parsed_org_id = uuid.UUID(org_id)
        parsed_user_id = uuid.UUID(user_id)
    except ValueError as exc:
        raise JWTError("Token contains a malformed identity UUID") from exc
    return AuthenticatedPrincipal(
        username=sub,
        organisation_id=parsed_org_id,
        user_id=parsed_user_id,
        org_role=org_role,
    )


def create_ws_token(
    subject: str,
    secret_key: str,
    *,
    organisation_id: str,
    user_id: str,
    org_role: str,
) -> str:
    """Short-lived JWT for WebSocket authentication (15 minute TTL)."""
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "org_id": organisation_id,
        "user_id": user_id,
        "org_role": org_role,
        "purpose": "ws",
        "iat": now,
        "exp": now + timedelta(minutes=_WS_TOKEN_MINUTES),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def decode_refresh_token_claims(token: str, secret_key: str) -> dict[str, object]:
    """Decode a refresh token and return raw claims including family/sequence."""
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    purpose = payload.get("purpose")
    if purpose != "refresh":
        raise JWTError("Token is not a refresh token")
    return payload


_CLAIM_TOKEN_MINUTES = 15


def create_claim_token(
    subject: str,
    secret_key: str,
    *,
    run_id: str,
    gate_id: str,
    client_id: str,
    expiry_minutes: int = _CLAIM_TOKEN_MINUTES,
) -> str:
    """Short-lived JWT scoped to a specific HITL gate claim.

    The token encodes ``run_id``, ``gate_id``, and ``client_id`` (the
    claimant) so that approve/reject can verify the claim scope without a
    separate DB lookup of who claimed the gate.
    """
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "purpose": "claim_token",
        "run_id": run_id,
        "gate_id": gate_id,
        "client_id": client_id,
        "iat": now,
        "exp": now + timedelta(minutes=expiry_minutes),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def decode_claim_token(
    token: str,
    secret_key: str,
    *,
    run_id: str,
    gate_id: str,
) -> dict[str, object]:
    """Validate a claim-token JWT and return its payload.

    Checks:
    * Signature + expiry (via ``jwt.decode``).
    * ``purpose == "claim_token"``.
    * ``run_id`` and ``gate_id`` match the expected values.

    Returns the full payload dict on success.
    """
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    purpose = payload.get("purpose")
    if purpose != "claim_token":
        raise JWTError(f"Token purpose '{purpose}' is not 'claim_token'")
    actual_run = payload.get("run_id")
    if actual_run != run_id:
        raise JWTError("claim_token run_id mismatch")
    actual_gate = payload.get("gate_id")
    if actual_gate != gate_id:
        raise JWTError("claim_token gate_id mismatch")
    return payload
