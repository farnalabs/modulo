"""API key generation and validation for MCP clients.

Key format:  mk_<8-char-prefix>_<32-char-secret>
Storage:     lookup_prefix = first 8 chars after "mk_"
             hashed_secret = SHA-256 hex of full key (constant-time compare)

Alpha scopes:
  operator — read + trigger + HITL
  runner   — trigger only

Team-scoped enforcement:
  When an API key has a non-null ``team_id``, all operations performed
  with that key are scoped to that specific team. The key's ``role`` field
  already limits the effective permission level (operator/runner). The
  ``team_id`` on the key acts as an additional filter — RLS policies on
  team-scoped tables enforce that only the owning team's data is accessible.
"""

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.api_key import OrgApiKey

_log = logging.getLogger(__name__)


class ApiKeyInvalidError(PermissionError):
    def __init__(self, detail: str = "API key is invalid or revoked") -> None:
        super().__init__(detail)


class ApiKeyNotFoundError(KeyError):
    pass


_PREFIX_LEN = 8
_SECRET_LEN = 32  # url-safe base64 chars
_MK_PREFIX = "mk_"


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, lookup_prefix, hashed_secret).

    full_key   — returned once to the caller; never stored
    lookup_prefix — stored in DB for fast lookup
    hashed_secret — SHA-256 hex of full_key; stored for verification
    """
    rand = secrets.token_urlsafe(_SECRET_LEN)[:_SECRET_LEN]
    prefix = rand[:_PREFIX_LEN]
    full_key = f"{_MK_PREFIX}{rand}"
    hashed = hashlib.sha256(full_key.encode()).hexdigest()
    return full_key, prefix, hashed


def _hash_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def _validate_team_key_role(key: OrgApiKey) -> None:
    """Reject admin roles on team-scoped keys.

    Team-scoped keys must use operator or runner roles — admin
    is reserved for org-wide keys without team_id.
    """
    if key.team_id is not None and key.role == "admin":
        raise ApiKeyInvalidError("team-scoped API keys cannot have admin role")


async def create_api_key(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    role: str,
    account_id: uuid.UUID,
    team_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> tuple[OrgApiKey, str]:
    """Create an API key. Returns (OrgApiKey, full_key). full_key is shown once."""
    full_key, prefix, hashed = generate_api_key()
    key = OrgApiKey(
        organisation_id=org_id,
        name=name,
        lookup_prefix=prefix,
        hashed_secret=hashed,
        role=role,
        account_id=account_id,
        team_id=team_id,
        expires_at=expires_at,
    )
    if team_id is not None:
        _validate_team_key_role(key)
    session.add(key)
    await session.flush()
    return key, full_key


async def validate_api_key(
    session: AsyncSession,
    full_key: str,
    org_id: uuid.UUID | None = None,
) -> OrgApiKey:
    """Validate a full API key.  Raises ApiKeyInvalidError on any failure.

    When *org_id* is ``None`` the lookup is scoped only by prefix (useful
    when the caller needs to resolve the organisation from the key record
    itself).
    """
    if not full_key.startswith(_MK_PREFIX):
        raise ApiKeyInvalidError()

    inner = full_key[len(_MK_PREFIX) :]
    prefix = inner[:_PREFIX_LEN]

    now = datetime.now(UTC)
    filters = [
        OrgApiKey.lookup_prefix == prefix,
        OrgApiKey.revoked_at.is_(None),
    ]
    if org_id is not None:
        filters.append(OrgApiKey.organisation_id == org_id)
    result = await session.execute(
        select(OrgApiKey).where(*filters)
    )
    key = result.scalar_one_or_none()
    if key is None:
        _log.info("api_key.not_found", extra={"prefix": prefix, "org_id": str(org_id) if org_id else None})
        raise ApiKeyInvalidError()
    if key.expires_at is not None and key.expires_at < now:
        _log.info("api_key.expired", extra={"key_id": str(key.id)})
        raise ApiKeyInvalidError()

    # Constant-time compare to prevent timing attacks
    expected = key.hashed_secret
    actual = _hash_key(full_key)
    if not hmac.compare_digest(expected, actual):
        raise ApiKeyInvalidError()

    # Update last_used_at
    await session.execute(update(OrgApiKey).where(OrgApiKey.id == key.id).values(last_used_at=datetime.now(UTC)))
    return key


async def revoke_api_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    org_id: uuid.UUID,
) -> bool:
    """Revoke an API key. Returns True if the key was found and revoked."""
    result = await session.execute(
        select(OrgApiKey).where(
            OrgApiKey.id == key_id,
            OrgApiKey.organisation_id == org_id,
            OrgApiKey.revoked_at.is_(None),
        )
    )
    key = result.scalar_one_or_none()
    if key is None:
        _log.info("api_key.revoke_not_found", extra={"key_id": str(key_id), "org_id": str(org_id)})
        return False
    key.revoked_at = datetime.now(UTC)
    await session.flush()
    _log.info("api_key.revoked", extra={"key_id": str(key.id)})
    return True


def _serialize_key(k: OrgApiKey) -> dict[str, Any]:
    now = datetime.now(UTC)
    is_active = k.revoked_at is None and (k.expires_at is None or k.expires_at > now)
    return {
        "id": str(k.id),
        "name": k.name,
        "role": k.role,
        "team_id": str(k.team_id) if k.team_id else None,
        "lookup_prefix": f"{_MK_PREFIX}{k.lookup_prefix}****",
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat(),
        "expires_at": k.expires_at.isoformat() if k.expires_at else None,
        "is_active": is_active,
    }


async def list_api_keys(
    session: AsyncSession,
    org_id: uuid.UUID,
    include_revoked: bool = False,
) -> list[dict[str, Any]]:
    """List API keys for an organisation, ordered by creation date descending."""
    stmt = select(OrgApiKey).where(OrgApiKey.organisation_id == org_id)
    if not include_revoked:
        stmt = stmt.where(OrgApiKey.revoked_at.is_(None))
    stmt = stmt.order_by(OrgApiKey.created_at.desc())
    result = await session.execute(stmt)
    keys = list(result.scalars())
    return [_serialize_key(k) for k in keys]


async def update_api_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    name: str | None = None,
    role: str | None = None,
    team_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> OrgApiKey | None:
    """Update an API key's metadata. Returns None if the key was not found."""
    stmt = select(OrgApiKey).where(
        OrgApiKey.id == key_id,
        OrgApiKey.organisation_id == org_id,
        OrgApiKey.revoked_at.is_(None),
    )
    result = await session.execute(stmt)
    key = result.scalar_one_or_none()
    if key is None:
        _log.info("api_key.update_not_found", extra={"key_id": str(key_id), "org_id": str(org_id)})
        return None
    if name is not None:
        key.name = name
    if role is not None:
        key.role = role
    if team_id is not None:
        key.team_id = team_id
        _validate_team_key_role(key)
    if expires_at is not None:
        key.expires_at = expires_at
    await session.flush()
    return key
