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

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.api_key import OrgApiKey
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)


class ApiKeyInvalidError(PermissionError):
    def __init__(self, detail: str = "API key is invalid or revoked") -> None:
        super().__init__(detail)


_PREFIX_LEN = 8
_SECRET_LEN = 32  # url-safe base64 chars
_MK_PREFIX = "mk_"

_UNSET = object()  # sentinel: ``team_id`` not provided in an update payload


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
    """Return SHA-256 hex digest of *full_key*."""
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
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(days=365)
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


async def mint_run_api_key(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    account_id: uuid.UUID,
    ttl_seconds: int,
) -> tuple[OrgApiKey, str] | None:
    """Mint a short-TTL RUNNER-ROLE API key for a script-mode sandbox run.

    FAR-296 Phase 3b: the key gives the script a restricted identity to call
    the Modulo API. Runner is the tightest role that can trigger/list runs; it
    also grants run.cancel, run.evals, api_key.create/update/revoke,
    hitl.claim/list, library.copy, and housekeeping.list — NOT
    pipeline/connector/secret access. Escalation risk from a leaked key (e.g.
    minting further runner-scope keys) is mitigated by the short TTL (clamped
    to ``[300, 86400]``s), per-run linkage, and revocation at run finalization.
    Fail-open: the caller decides whether a failed mint blocks the dispatch
    (it should NOT — the sandbox runs without the key rather than failing).
    """
    ttl_seconds = max(300, min(ttl_seconds, 86400))
    try:
        key, full_key = await create_api_key(
            session,
            org_id=org_id,
            name=f"run:{run_id}:node:{node_id}",
            role="runner",
            account_id=account_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        key.run_id = run_id
        await session.flush()
        return key, full_key
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "api_key.mint_run_failed",
            extra={"run_id": str(run_id), "node_id": node_id, "org_id": str(org_id)},
        )
        return None


async def revoke_run_api_key(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
) -> int:
    """Revoke all per-run API keys linked to a run (FAR-296 Phase 3b).

    Returns the number of keys revoked. Uses the ``run_id`` linkage column.
    """
    from sqlalchemy.engine import CursorResult

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(OrgApiKey)
            .where(OrgApiKey.organisation_id == org_id, OrgApiKey.run_id == run_id, OrgApiKey.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        ),
    )
    return result.rowcount or 0


async def revoke_run_api_key_sweep(
    session_factory: Callable[..., Any],
    *,
    org_ids: list[uuid.UUID] | None = None,
    batch_size: int = 50,
    budget_seconds: float = 30.0,
) -> dict[str, int]:
    """Compensating revocation sweep for per-run runner-role API keys (FAR-296 Phase 3b-2).

    Finds per-run keys (``run_id`` NOT NULL, ``revoked_at`` IS NULL) whose runs
    are TERMINAL but were terminalized by a NON-executor path (dispatcher_reconcile
    raw-SQL terminalizers, saq_hooks._mark_run_failed, transition_run,
    request_cancellation) and revokes them. The executor path already revokes at
    finalization; this sweep is the compensating backstop so keys don't leak.

    Bounded: ``batch_size`` keys per org, ``budget_seconds`` wall-clock budget,
    never raises. Runs system-scoped (modulo_system role, LOGIN, BYPASSRLS — the
    dedicated cross-org system cron role); the sweep processes each org under its
    own RLS context when ``org_ids`` is None (self-selects all orgs).

    Returns ``{"scanned", "revoked", "errors"}``.
    """
    deadline = time.monotonic() + budget_seconds
    scanned = 0
    revoked = 0
    errors = 0
    try:
        if org_ids is None:
            # Org self-selection runs system-scoped, but the factory's sessions
            # are autobegin=False (the DI default), so the SELECT needs an
            # explicit begin() — a bare execute would raise InvalidRequestError
            # and be swallowed into errors (this org-selection path is the ONLY
            # production path via dispatcher_reconcile).
            async with session_factory() as session, session.begin():
                result = await session.execute(select(Organisation.id))
                org_ids = list(result.scalars())
        for org_id in org_ids:
            if time.monotonic() > deadline:
                break
            try:
                async with session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    key_rows = (
                        await session.execute(
                            select(OrgApiKey.id, OrgApiKey.run_id)
                            .join(Run, Run.id == OrgApiKey.run_id)
                            .where(
                                OrgApiKey.organisation_id == org_id,
                                OrgApiKey.run_id.is_not(None),
                                OrgApiKey.revoked_at.is_(None),
                                Run.status.in_(sorted(TERMINAL_STATUSES)),
                            )
                            .limit(batch_size)
                        )
                    ).all()
                    for _key_id, key_run_id in key_rows:
                        scanned += 1
                        revoked += await revoke_run_api_key(session, run_id=key_run_id, org_id=org_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                errors += 1
                _log.exception("api_key.revoke_run_sweep_org_failed", extra={"org_id": str(org_id)})
    except asyncio.CancelledError:
        raise
    except Exception:
        errors += 1
        _log.exception("api_key.revoke_run_sweep_failed")
    return {"scanned": scanned, "revoked": revoked, "errors": errors}


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
        raise ApiKeyInvalidError

    inner = full_key[len(_MK_PREFIX) :]
    prefix = inner[:_PREFIX_LEN]

    now = datetime.now(UTC)
    filters = [
        OrgApiKey.lookup_prefix == prefix,
        OrgApiKey.revoked_at.is_(None),
    ]
    if org_id is not None:
        filters.append(OrgApiKey.organisation_id == org_id)
    result = await session.execute(select(OrgApiKey).where(*filters))
    keys = list(result.scalars())
    if not keys:
        _log.info("api_key.not_found", extra={"prefix": prefix, "org_id": str(org_id) if org_id else None})
        raise ApiKeyInvalidError

    actual_hash = _hash_key(full_key)
    for key in keys:
        if key.expires_at is not None and key.expires_at < now:
            continue
        if hmac.compare_digest(key.hashed_secret, actual_hash):
            key.last_used_at = datetime.now(UTC)
            await session.flush()
            return key

    raise ApiKeyInvalidError


async def revoke_api_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    org_id: uuid.UUID,
) -> bool:
    """Revoke an API key. Returns True if the key was found and revoked.

    The key row is locked with ``FOR UPDATE`` so two concurrent revocations
    serialise: the second waits for the first to commit, re-reads the row with
    ``revoked_at`` already set (the ``revoked_at IS NULL`` filter excludes it)
    and returns False instead of racing on the same row.
    """
    result = await session.execute(
        select(OrgApiKey)
        .where(
            OrgApiKey.id == key_id,
            OrgApiKey.organisation_id == org_id,
            OrgApiKey.revoked_at.is_(None),
        )
        .with_for_update()
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
    """Build a serialisable dict from an OrgApiKey for API responses.

    Masks the secret portion of the key and adds a computed ``is_active``
    field based on revocation and expiration state.
    """
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
    team_id: uuid.UUID | object | None = _UNSET,
    expires_at: datetime | None = None,
) -> OrgApiKey | None:
    """Update an API key's metadata. Returns None if the key was not found.

    ``team_id`` accepts three states:
    - ``_UNSET`` (default): leave the current team scope unchanged.
    - ``None``: clear the team scope (team-scoped key becomes org-wide).
    - a ``uuid.UUID``: scope the key to that team.
    """
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
    if role is not None or team_id is not _UNSET:
        effective_role = role if role is not None else key.role
        # team_id is the NEW scope when provided: None clears it (org-wide),
        # a UUID scopes it; _UNSET keeps the current scope.
        effective_team_id = key.team_id if team_id is _UNSET else team_id
        if effective_team_id is not None and effective_role == "admin":
            raise ApiKeyInvalidError("team-scoped API keys cannot have admin role")
    if role is not None:
        key.role = role
    if team_id is not _UNSET:
        key.team_id = cast(uuid.UUID | None, team_id)
    if expires_at is not None:
        key.expires_at = expires_at
    await session.flush()
    return key
