"""JWT utilities for Modulo v1 user management.

Always uses HS256. The `none` algorithm is excluded from decode's allowed list,
so tokens signed with `alg: none` are rejected by PyJWT before we see them.

Token families: Each refresh token belongs to a family. On refresh, the sequence
number is incremented. If a stale sequence is presented (token theft), the entire
family is blacklisted. On logout, the family is explicitly invalidated.
"""

import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError as JWTError

_log = logging.getLogger(__name__)

_ALGORITHM: str = "HS256"
_ACCESS_TOKEN_MINUTES: int = 15
_REFRESH_TOKEN_HOURS: int = 168
_WS_TOKEN_MINUTES: int = 15

#: FAR-634: the credential class stamped on every access/refresh JWT at mint
#: time. ``browser`` = minted by an interactive login flow (password login,
#: demo login, SSO callback, refresh rotation of a browser family);
#: ``programmatic`` = minted for an API-key/automation exchange path. The
#: human_only HITL enforcement (REST ``_enforce_human_only_gate``) denies
#: principals whose ``client_kind != "browser"``. Tokens minted BEFORE this
#: claim existed carry no ``client_kind`` and decode as ``browser`` (backward
#: compatible default).
CLIENT_KIND_BROWSER: str = "browser"
CLIENT_KIND_PROGRAMMATIC: str = "programmatic"
_CLIENT_KIND_CLAIM: str = "client_kind"
#: DECODE-SIDE ONLY: the claim default for legacy tokens minted before the
#: claim existed. Mint-side, ``client_kind`` is a REQUIRED parameter on
#: ``create_access_token``/``create_refresh_token`` (FAR-634 review) so a
#: future programmatic mint path cannot silently inherit the browser class.
_DEFAULT_CLIENT_KIND: str = CLIENT_KIND_BROWSER


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Identity and tenant claims from a verified access token."""

    username: str
    organisation_id: uuid.UUID | None
    account_id: uuid.UUID
    org_role: str | None
    is_system_admin: bool = False
    #: FAR-610: True when the credential was an org API key (``mk_``) rather
    #: than a browser-login JWT. Human_only HITL gates deny API-key principals
    #: on decision actions (approve / approve-with-modification / deliver-manual
    #: / submit-manual); browser JWTs pass. JWTs carry no client-type claim, so
    #: this marker is the only reliable credential-kind signal — the API-key
    #: resolution path (``get_current_tenant_user_or_api_key``) sets it True.
    via_api_key: bool = False
    #: FAR-634: the credential class from the token's ``client_kind`` claim
    #: (``CLIENT_KIND_BROWSER`` default for legacy tokens minted before the
    #: claim existed). Enforcement denies anything that is not exactly
    #: ``CLIENT_KIND_BROWSER`` (fail closed for unknown kinds). Kept beside
    #: ``via_api_key``: an API-key principal carries both (``via_api_key``
    #: marks the credential TYPE for its consumers; ``client_kind`` is the
    #: JWT-level class the human_only gate reads).
    client_kind: str = _DEFAULT_CLIENT_KIND

    @property
    def user_id(self) -> uuid.UUID:
        return self.account_id


class TenantPrincipal(AuthenticatedPrincipal):
    """Authenticated principal with validated tenant-scoped claims."""

    organisation_id: uuid.UUID
    org_role: str


def create_access_token(
    subject: str,
    secret_key: str,
    *,
    organisation_id: str,
    account_id: str = "",
    org_role: str,
    is_system_admin: bool = False,
    user_id: str = "",
    ttl_minutes: int | None = None,
    client_kind: str,
) -> str:
    """Access token with a configurable TTL (default 15 minutes).

    ``client_kind`` (FAR-634) stamps the credential class: ``CLIENT_KIND_BROWSER``
    for tokens minted by an interactive login flow, ``CLIENT_KIND_PROGRAMMATIC``
    for tokens minted for API-key/automation exchange paths. REQUIRED (no
    default) so a future programmatic mint path cannot silently inherit the
    browser class and pass the human_only HITL gate — every mint site must
    state the credential class explicitly at issuance. HONEST LIMITATION
    (defense-in-depth, not absolute): agent sessions in this org hold the
    admin password and mint through the same login endpoint, so their tokens
    are indistinguishable from a browser login at issuance — the FAR-611 sweep
    alarm is the detective control for that residual. Step-up re-auth is a
    separate (bigger) product decision.
    """
    resolved_account_id: str = account_id or user_id
    now: datetime = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": subject,
        "org_id": organisation_id,
        "account_id": resolved_account_id,
        "org_role": org_role,
        "is_system_admin": is_system_admin,
        _CLIENT_KIND_CLAIM: client_kind,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes if ttl_minutes is not None else _ACCESS_TOKEN_MINUTES),
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
    client_kind: str,
) -> str:
    """7-day refresh token with family+sequence for rotation detection.

    ``client_kind`` (FAR-634) rides along so rotation propagates the ORIGINAL
    credential class: the refresh endpoint re-stamps it onto the rotated
    access+refresh pair (a browser family never silently becomes a
    programmatic one and vice versa). REQUIRED (no default) — same fail-closed
    contract as :func:`create_access_token`: a mint site must state the
    credential class explicitly, never silently inherit ``browser``.
    """
    resolved_account_id: str = account_id or user_id
    now: datetime = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": subject,
        "org_id": organisation_id,
        "account_id": resolved_account_id,
        "org_role": org_role,
        "is_system_admin": is_system_admin,
        "purpose": "refresh",
        "token_family": token_family,
        "token_sequence": token_sequence,
        _CLIENT_KIND_CLAIM: client_kind,
        "iat": now,
        "exp": now + timedelta(hours=_REFRESH_TOKEN_HOURS),
    }
    return str(jwt.encode(claims, secret_key, algorithm=_ALGORITHM))


def refresh_access_token(refresh_token: str, secret_key: str) -> str:
    """Validate a refresh token and issue a new access token."""
    principal: AuthenticatedPrincipal = decode_principal(refresh_token, secret_key, allowed_purposes=["refresh"])
    return create_access_token(
        principal.username,
        secret_key,
        organisation_id=str(principal.organisation_id) if principal.organisation_id else "",
        account_id=str(principal.account_id),
        org_role=principal.org_role or "",
        is_system_admin=principal.is_system_admin,
        client_kind=principal.client_kind,
    )


def decode_principal(token: str, secret_key: str, allowed_purposes: list[str] | None = None) -> AuthenticatedPrincipal:
    """Decode and validate all identity claims needed for tenant-scoped API access."""
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    sub: object = payload.get("sub")
    org_id: object = payload.get("org_id")
    account_id: object = payload.get("account_id") or payload.get("user_id")
    org_role: object = payload.get("org_role")
    is_system_admin: object = payload.get("is_system_admin", False)
    # FAR-634: the credential class. ABSENT claim (tokens minted before the
    # claim existed) -> the browser default (backward compatible). A present
    # but non-string value is carried as its string form so enforcement sees
    # a value that is not exactly CLIENT_KIND_BROWSER (fail closed) rather
    # than silently re-classifying an unparseable claim as browser.
    raw_client_kind: object = payload.get(_CLIENT_KIND_CLAIM)
    if raw_client_kind is None:
        client_kind: str = _DEFAULT_CLIENT_KIND
    elif isinstance(raw_client_kind, str):
        client_kind = raw_client_kind
    else:
        _log.warning("jwt.non_string_client_kind", extra={"value": str(raw_client_kind)})
        client_kind = str(raw_client_kind)
    if not isinstance(sub, str) or not sub:
        raise JWTError("Token missing or invalid 'sub' claim")
    if not isinstance(account_id, str):
        raise JWTError("Token missing or invalid 'account_id' claim")
    if not isinstance(is_system_admin, bool):
        _log.warning("jwt.non_bool_is_system_admin", extra={"value": str(is_system_admin)})
        is_system_admin = False
    if allowed_purposes is not None:
        purpose: object = payload.get("purpose")
        if not isinstance(purpose, str) or purpose not in allowed_purposes:
            raise JWTError(f"Token purpose '{purpose}' not in allowed list: {allowed_purposes}")
    try:
        parsed_account_id: uuid.UUID = uuid.UUID(account_id)
    except ValueError as exc:
        raise JWTError("Token contains a malformed identity UUID") from exc
    parsed_org_id: uuid.UUID | None = None
    if isinstance(org_id, str) and org_id:
        with contextlib.suppress(ValueError):
            parsed_org_id = uuid.UUID(org_id)
    parsed_org_role: str | None = org_role if isinstance(org_role, str) and org_role else None
    if org_id is not None and parsed_org_id is None:
        _log.warning("jwt.malformed_org_id", extra={"org_id": str(org_id)})
    return AuthenticatedPrincipal(
        username=sub,
        organisation_id=parsed_org_id,
        account_id=parsed_account_id,
        org_role=parsed_org_role,
        is_system_admin=is_system_admin,
        client_kind=client_kind,
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
    resolved_account_id: str = account_id or user_id
    now: datetime = datetime.now(UTC)
    claims: dict[str, object] = {
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
    purpose: object = payload.get("purpose")
    if purpose != "refresh":
        raise JWTError("Token is not a refresh token")
    return payload


_CLAIM_TOKEN_MINUTES: int = 15


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
    now: datetime = datetime.now(UTC)
    claims: dict[str, object] = {
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
    expected_client_id: str | None = None,
) -> dict[str, object]:
    """Validate a claim-token JWT and return its payload.

    Checks:
    * Signature + expiry (via ``jwt.decode``).
    * ``purpose == "claim_token"``.
    * ``run_id``, ``gate_id``, and optionally ``client_id`` match the expected values.

    Returns the full payload dict on success.
    """
    payload: dict[str, object] = jwt.decode(token, secret_key, algorithms=[_ALGORITHM])
    purpose: object = payload.get("purpose")
    if purpose != "claim_token":
        raise JWTError(f"Token purpose '{purpose}' is not 'claim_token'")
    actual_run: object = payload.get("run_id")
    if actual_run != run_id:
        raise JWTError("claim_token run_id mismatch")
    actual_gate: object = payload.get("gate_id")
    if actual_gate != gate_id:
        raise JWTError("claim_token gate_id mismatch")
    if expected_client_id is not None:
        actual_client: object = payload.get("client_id")
        if actual_client != expected_client_id:
            raise JWTError("claim_token client_id mismatch")
    return payload
