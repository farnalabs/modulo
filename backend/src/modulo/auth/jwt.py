"""JWT utilities for Modulo v1 user management.

Always uses HS256. The `none` algorithm is excluded from decode's allowed list,
so tokens signed with `alg: none` are rejected by python-jose before we see them.

Token families: Each refresh token belongs to a family. On refresh, the sequence
number is incremented. If a stale sequence is presented (token theft), the entire
family is blacklisted. On logout, the family is explicitly invalidated.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

_log = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_ACCESS_TOKEN_MINUTES = 15
_REFRESH_TOKEN_HOURS = 168
_WS_TOKEN_MINUTES = 15


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Identity and tenant claims from a verified access token."""

    username: str
    organisation_id: uuid.UUID | None
    account_id: uuid.UUID
    org_role: str | None
    is_system_admin: bool = False

    @property
    def user_id(self) -> uuid.UUID:
        return self.account_id


def _resolve_account_id(*, account_id: str = "", user_id: str = "") -> str:
    return account_id or user_id


def create_access_token(
    subject: str,
    secret_key: str,
    *,
    organisation_id: str,
    account_id: str = "",
    org_role: str,
    is_system_admin: bool = False,
    user_id: str = "",
) -> str:
    """15-minute access token."""
    resolved_account_id = _resolve_account_id(account_id=account_id, user_id=user_id)
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "org_id": organisation_id,
        "account_id": resolved_account_id,
        "org_role": org_role,
        "is_system_admin": is_system_admin,
        "iat": now,
        "exp": now + timedelta(minutes=_ACCESS_TOKEN_MINUTES),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def create_refresh_token(
    subject: str,
    secret_key: str,
    *,
    organisation_id: str,
    account_id: str = "",
    org_role: str,
    is_system_admin: bool = False,
    token_family: str,
    token_sequence: int,
    user_id: str = "",
) -> str:
    """7-day refresh token with family+sequence for rotation detection."""
    resolved_account_id = _resolve_account_id(account_id=account_id, user_id=user_id)
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "org_id": organisation_id,
        "account_id": resolved_account_id,
        "org_role": org_role,
        "is_system_admin": is_system_admin,
        "purpose": "refresh",
        "token_family": token_family,
        "token_sequence": token_sequence,
        "iat": now,
        "exp": now + timedelta(hours=_REFRESH_TOKEN_HOURS),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def refresh_access_token(refresh_token: str, secret_key: str) -> str:
    """Validate a refresh token and issue a new access token."""
    principal = decode_principal(refresh_token, secret_key, allowed_purposes=["refresh"])
    return create_access_token(
        principal.username,
        secret_key,
        organisation_id=str(principal.organisation_id) if principal.organisation_id else "",
        account_id=str(principal.account_id),
        org_role=principal.org_role or "",
        is_system_admin=principal.is_system_admin,
    )


def decode_principal(token: str, secret_key: str, allowed_purposes: list[str] | None = None) -> AuthenticatedPrincipal:
    """Decode and validate all identity claims needed for tenant-scoped API access."""
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    sub = payload.get("sub")
    org_id = payload.get("org_id")
    account_id = payload.get("account_id") or payload.get("user_id")
    org_role = payload.get("org_role")
    is_system_admin = payload.get("is_system_admin", False)
    if not isinstance(sub, str) or not sub:
        raise JWTError("Token missing or invalid 'sub' claim")
    if not isinstance(account_id, str):
        raise JWTError("Token missing or invalid 'account_id' claim")
    if not isinstance(is_system_admin, bool):
        _log.warning("jwt.non_bool_is_system_admin", extra={"value": str(is_system_admin)})
        is_system_admin = False
    if allowed_purposes is not None:
        purpose = payload.get("purpose")
        if not isinstance(purpose, str) or purpose not in allowed_purposes:
            raise JWTError(f"Token purpose '{purpose}' not in allowed list: {allowed_purposes}")
    try:
        parsed_account_id = uuid.UUID(account_id)
    except ValueError as exc:
        raise JWTError("Token contains a malformed identity UUID") from exc
    parsed_org_id: uuid.UUID | None = None
    if isinstance(org_id, str) and org_id:
        try:
            parsed_org_id = uuid.UUID(org_id)
        except ValueError:
            pass
    parsed_org_role: str | None = org_role if isinstance(org_role, str) and org_role else None
    if org_id is not None and parsed_org_id is None and isinstance(org_id, str):
        _log.warning("jwt.malformed_org_id", extra={"org_id": org_id})
    return AuthenticatedPrincipal(
        username=sub,
        organisation_id=parsed_org_id,
        account_id=parsed_account_id,
        org_role=parsed_org_role,
        is_system_admin=is_system_admin,
    )


def create_ws_token(
    subject: str,
    secret_key: str,
    *,
    organisation_id: str,
    account_id: str = "",
    org_role: str,
    is_system_admin: bool = False,
    user_id: str = "",
    ttl_minutes: int | None = None,
) -> str:
    """Short-lived JWT for WebSocket authentication (15 minute TTL by default)."""
    resolved_account_id = _resolve_account_id(account_id=account_id, user_id=user_id)
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "org_id": organisation_id,
        "account_id": resolved_account_id,
        "org_role": org_role,
        "is_system_admin": is_system_admin,
        "purpose": "ws",
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes if ttl_minutes is not None else _WS_TOKEN_MINUTES),
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
