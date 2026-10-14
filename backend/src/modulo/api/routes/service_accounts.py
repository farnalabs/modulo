"""Service-account provisioning and lifecycle (FAR-1478, ADR 056 / ADR 058).

Slice 2 of 3 makes a service principal usable: an org admin creates one with
its own name, assigns it an org role (ceiling ``operator``) and team
memberships (via the existing ``/api/v1/teams/{id}/members`` surface), and
keys are minted with the SERVICE principal as their owner — never the human
who pressed the button.

Shape decisions, each traceable to the ADRs:

* **Column, not a table** (ADR 058): a service account is an ``accounts`` row
  with ``kind='service'``; everything below reads/writes that row through the
  shared account machinery.
* **Org-owned, never super-principal**: every role write goes through
  :func:`_validate_service_org_role`, whose ceiling authority is
  :func:`modulo.core.principals.clamp_service_role` — a grant that would be
  clamped (``admin``) is REJECTED with a 422, never silently widened, and the
  database trigger from migration 0311 backstops every other write path
  structurally.
* **Key ownership is the service principal**: the mint endpoint stamps
  ``org_api_keys.account_id`` with the target service account. A service
  principal mints its own keys with its own ``mk_`` credential; a human admin
  may bootstrap one for it. Either way the owner row is the service account,
  so the key dies with the service principal's decommission — not with a
  human's offboarding.
* **Decommission is explicit** (ADR 058): one-way retirement that stamps
  ``accounts.decommissioned_at``, tombstones the org membership, flips
  ``active`` off and bulk-revokes every live key. It is never triggered by
  owner death, and the human admin surface refuses service targets outright
  (``admin.py``) so a decommission can never be undone through the
  reactivation path.
* **Registry-level exclusion only** (FAR-1477 / ADR 058): grant-sets offered
  on a service key flow through ``enforce_grants_mint_cap_for`` →
  ``is_delegable``, the ONE live registry flag. This module never keeps a
  second exclusion list.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    deny_break_glass_mint_any_credential,
    get_db_session,
    require_permission_any_credential,
)
from modulo.api.routes.api_keys import enforce_grants_mint_cap_for
from modulo.auth.api_key import (
    ApiKeyGrantsUnavailableError,
    ApiKeyTtlError,
    api_key_grants_enabled,
    create_api_key,
)
from modulo.auth.dependencies import (
    get_current_tenant_user,
    get_current_tenant_user_or_api_key,
    resolve_role_from_membership,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.team_rbac import VALID_ORG_ROLES, org_role_level
from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.principals import KIND_SERVICE, clamp_service_role, is_service_account
from modulo.db.crud.account import create_service_account, get_account_by_id
from modulo.db.crud.org_membership import create_membership, get_membership_by_account_and_org
from modulo.db.models.account import Account
from modulo.db.models.api_key import OrgApiKey
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.team_membership import TeamMembership
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_CREATE = "service_accounts.create_service_account_endpoint"
_CODE_LIST = "service_accounts.list_service_accounts_endpoint"
_CODE_GET = "service_accounts.get_service_account_endpoint"
_CODE_UPDATE = "service_accounts.update_service_account_endpoint"
_CODE_DECOMMISSION = "service_accounts.decommission_service_account_endpoint"
_CODE_MINT_KEY = "service_accounts.create_service_account_key_endpoint"

_MSG_NOT_FOUND = "Service account not found"
_MSG_DECOMMISSIONED = "Service account is decommissioned"
_MSG_ACTIVE_MEMBERSHIP_REQUIRED = "Active organisation membership required to manage API keys"
_MSG_AUDIT_CREATE_FAILED = "service_accounts.create_audit_failed"
_MSG_AUDIT_UPDATE_FAILED = "service_accounts.update_audit_failed"
_MSG_AUDIT_DECOMMISSION_FAILED = "service_accounts.decommission_audit_failed"
_MSG_AUDIT_KEY_FAILED = "service_accounts.key_audit_failed"

_KEY_ROLES = ("operator", "runner")

router = APIRouter(prefix="/api/v1/service-accounts", tags=["service-accounts"])


def _require_admin(principal: TenantPrincipal, action: str) -> None:
    """Raise 403 unless the principal holds the org ``admin`` role."""
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only admin users can {action}",
        )


def _normalise_name(name: str) -> str:
    """Strip whitespace from a display name, rejecting blank values."""
    stripped = name.strip()
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Service account name must not be blank",
        )
    return stripped


def _validate_service_org_role(role: str) -> str:
    """Validate a role for a service principal against the operator ceiling.

    The vocabulary is the org-role set (so a request for ``admin`` reaches the
    domain rule and gets a meaningful message instead of a pydantic pattern
    error), and :func:`clamp_service_role` is the ceiling AUTHORITY: any role
    that would be clamped (``admin``) — and any unknown role, which clamps to
    the deny sentinel — is rejected with 422. The grant is never applied in
    clamped form: ADR 058 says a service principal NEVER holds admin, and a
    silently-admin-to-operator rewrite would hide the attempt instead of
    refusing it. Migration 0311's trigger is the structural backstop for any
    write path that skips this check.
    """
    if role not in VALID_ORG_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"role must be one of {sorted(VALID_ORG_ROLES)}",
        )
    if clamp_service_role(role) != role:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Service accounts cannot hold the 'admin' role — the ceiling is 'operator'",
        )
    return role


async def _load_service_account(
    session: AsyncSession,
    service_account_id: uuid.UUID,
    org_id: uuid.UUID,
) -> tuple[Account, OrgMembership]:
    """Load a service account that belongs to ``org_id`` (404 otherwise).

    A human account (or an account of another org) is indistinguishable from
    a missing one on this surface — the route is service-accounts-only and
    org-scoped, so either way the answer is the same 404.
    """
    account = await get_account_by_id(session, service_account_id)
    if account is None or not is_service_account(account):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    membership = await get_membership_by_account_and_org(session, service_account_id, org_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    return account, membership


def _assert_not_decommissioned(account: Account) -> None:
    """Refuse any further mutation of a retired service principal (409)."""
    if account.decommissioned_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_DECOMMISSIONED)


def _to_response(account: Account, role: str) -> ServiceAccountResponse:
    """Serialise a service account + its org role for the REST surface."""
    return ServiceAccountResponse(
        id=str(account.id),
        name=account.display_name,
        kind=account.kind,
        role=role,
        active=account.active,
        decommissioned_at=account.decommissioned_at.isoformat() if account.decommissioned_at else None,
        created_at=account.created_at.isoformat(),
    )


async def _list_service_accounts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int,
    page_size: int,
    search: str | None,
) -> tuple[list[tuple[Account, OrgMembership]], int]:
    """Org-scoped service accounts (``kind='service'``), newest data order."""
    conditions = [OrgMembership.organisation_id == org_id, Account.kind == KIND_SERVICE]
    if search:
        conditions.append(Account.display_name.ilike(f"%{search}%"))

    count_q = (
        select(func.count())
        .select_from(OrgMembership)
        .join(Account, Account.id == OrgMembership.account_id)
        .where(*conditions)
    )
    total = (await session.execute(count_q)).scalar() or 0

    query = (
        select(Account, OrgMembership)
        .join(Account, Account.id == OrgMembership.account_id)
        .where(*conditions)
        .order_by(Account.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(query)
    return [(row[0], row[1]) for row in result.all()], total


def _parse_future_expires_at(value: str | None) -> datetime | None:
    """Parse an optional ISO expiry, rejecting values not in the future."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="expires_at must be in the future",
        )
    return parsed


async def _resolve_grants_flag(org_id: uuid.UUID) -> None:
    """FAR-1477 gate: grant-sets are accepted only with the org flag ON.

    STRICT read: a transient flag-read failure must not silently downgrade to
    a legacy full-role key (which would WIDEN access) — it answers 503.
    """
    try:
        grants_on = await api_key_grants_enabled(org_id, strict=True)
    except ApiKeyGrantsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key grant settings are temporarily unavailable; retry shortly",
        ) from None
    if not grants_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="API key grant-sets are not enabled for this organisation",
        )


class ServiceAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    role: str = "operator"


class ServiceAccountUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    role: str | None = None


class ServiceAccountResponse(BaseModel):
    id: str
    name: str
    kind: str
    role: str
    active: bool
    decommissioned_at: str | None = None
    created_at: str | None = None


class ServiceAccountListResponse(BaseModel):
    items: list[ServiceAccountResponse]
    total: int
    page: int
    page_size: int


class ServiceAccountKeyCreate(BaseModel):
    name: str = Field(min_length=1)
    role: str = "operator"
    expires_at: str | None = None
    # FAR-1477: explicit grant-set, same tri-state semantics as POST
    # /api/v1/api-keys (None = legacy role bundle).
    grants: list[str] | None = None
    # Present ONLY so an explicit payload field can be rejected: a service
    # key is an org-level machine identity (scope is IMMUTABLE post-mint).
    scope: str | None = None


class ServiceAccountKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    key_value: str
    lookup_prefix: str
    created_at: datetime
    owner_account_id: uuid.UUID
    scope: str = "org"
    grants: list[str] | None = None

    model_config = {"from_attributes": False}


class ServiceAccountDecommissionResponse(BaseModel):
    id: str
    decommissioned_at: str
    revoked_keys: int
    team_memberships_removed: int


# ---------------------------------------------------------------------------
# Provisioning — create / list / read / rename / decommission
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(
            audited("api_access_post", "service_account", principal_dep=get_current_tenant_user),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg
        )
    ],
)
@handle_db_errors(_CODE_CREATE)
async def create_service_account_endpoint(
    req: ServiceAccountCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ServiceAccountResponse:
    """Create a service principal with its own name and an org role ≤ operator."""
    _require_admin(principal, "create service accounts")
    name = _normalise_name(req.name)
    role = _validate_service_org_role(req.role)

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        account = await create_service_account(session, display_name=name)
        membership = await create_membership(
            session,
            account_id=account.id,
            org_id=principal.organisation_id,
            role=role,
        )

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="service_account",
        event_type="service_account_created",
        resource_id=account.id,
        payload={"name": name, "role": membership.role},
        log_key=_MSG_AUDIT_CREATE_FAILED,
    )
    return _to_response(account, membership.role)


@router.get("")
@handle_db_errors(_CODE_LIST)
async def list_service_accounts_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, min_length=1),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ServiceAccountListResponse:
    """List the org's service principals (admins only — same floor as /admin/users)."""
    _require_admin(principal, "list service accounts")
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        rows, total = await _list_service_accounts(
            session,
            org_id=principal.organisation_id,
            page=page,
            page_size=page_size,
            search=search,
        )
    return ServiceAccountListResponse(
        items=[_to_response(account, membership.role) for account, membership in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{service_account_id}")
@handle_db_errors(_CODE_GET)
async def get_service_account_endpoint(
    service_account_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ServiceAccountResponse:
    """Read one service principal."""
    _require_admin(principal, "read service accounts")
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        account, membership = await _load_service_account(session, service_account_id, principal.organisation_id)
    return _to_response(account, membership.role)


@router.patch(
    "/{service_account_id}",
    dependencies=[
        Depends(
            audited("api_access_patch", "service_account", principal_dep=get_current_tenant_user),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg
        )
    ],
)
@handle_db_errors(_CODE_UPDATE)
async def update_service_account_endpoint(
    service_account_id: uuid.UUID,
    req: ServiceAccountUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ServiceAccountResponse:
    """Rename a service principal and/or assign a new org role (≤ operator)."""
    _require_admin(principal, "update service accounts")
    if req.name is None and req.role is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one of name or role must be provided",
        )
    name = _normalise_name(req.name) if req.name is not None else None
    role = _validate_service_org_role(req.role) if req.role is not None else None

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        account, membership = await _load_service_account(session, service_account_id, principal.organisation_id)
        _assert_not_decommissioned(account)
        if name is not None:
            account.display_name = name
        if role is not None:
            membership.role = role
        await session.flush()

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="service_account",
        event_type="service_account_updated",
        resource_id=account.id,
        payload={
            "name": account.display_name,
            "role": membership.role,
        },
        log_key=_MSG_AUDIT_UPDATE_FAILED,
    )
    return _to_response(account, membership.role)


@router.post(
    "/{service_account_id}/decommission",
    dependencies=[
        Depends(
            audited("api_access_post", "service_account", principal_dep=get_current_tenant_user),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg
        )
    ],
)
@handle_db_errors(_CODE_DECOMMISSION)
async def decommission_service_account_endpoint(
    service_account_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ServiceAccountDecommissionResponse:
    """Explicitly retire a service principal: tombstone, revoke keys, drop teams.

    One-way by design (ADR 058: decommissioned EXPLICITLY, never by owner
    death): the timestamp is durable, the membership tombstone makes every
    live-role read return None (so any surviving credential dies immediately),
    and every live ``mk_`` key owned by the principal is bulk-revoked in the
    same transaction. The human user surface refuses service targets, so the
    membership tombstone can never be cleared behind this endpoint's back.
    """
    _require_admin(principal, "decommission service accounts")

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        account, membership = await _load_service_account(session, service_account_id, principal.organisation_id)
        _assert_not_decommissioned(account)

        now = datetime.now(UTC)
        account.decommissioned_at = now
        account.active = False
        membership.deactivated_at = now

        from sqlalchemy.engine import CursorResult

        revoke_result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(OrgApiKey)
                .where(
                    OrgApiKey.account_id == account.id,
                    OrgApiKey.organisation_id == principal.organisation_id,
                    OrgApiKey.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            ),
        )
        revoked_keys = revoke_result.rowcount or 0

        team_rows = await session.execute(
            select(TeamMembership).where(
                TeamMembership.account_id == account.id,
                TeamMembership.organisation_id == principal.organisation_id,
            )
        )
        team_memberships_removed = 0
        for team_membership in team_rows.scalars():
            await session.delete(team_membership)
            team_memberships_removed += 1
        await session.flush()

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="service_account",
        event_type="service_account_decommissioned",
        resource_id=account.id,
        payload={
            "name": account.display_name,
            "revoked_keys": revoked_keys,
            "team_memberships_removed": team_memberships_removed,
        },
        log_key=_MSG_AUDIT_DECOMMISSION_FAILED,
    )
    return ServiceAccountDecommissionResponse(
        id=str(account.id),
        decommissioned_at=now.isoformat(),
        revoked_keys=revoked_keys,
        team_memberships_removed=team_memberships_removed,
    )


# ---------------------------------------------------------------------------
# Key minting — ownership is the SERVICE principal (ADR 056)
# ---------------------------------------------------------------------------


@router.post(
    "/{service_account_id}/keys",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        # any-credential variant: the service principal mints with its own
        # ``mk_`` key, so a JWT-only deny would 401 the self-mint path (FAR-681).
        Depends(deny_break_glass_mint_any_credential),
        Depends(
            audited(
                "api_access_post",
                "api_key",
                principal_dep=get_current_tenant_user_or_api_key,
            ),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg
        ),
    ],
)
@handle_db_errors(_CODE_MINT_KEY)
async def create_service_account_key_endpoint(
    service_account_id: uuid.UUID,
    req: ServiceAccountKeyCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission_any_credential("api_key.create"),
) -> ServiceAccountKeyResponse:
    """Mint an ``mk_`` key OWNED by the service principal.

    Two callers, one owner stamp:

    * a human admin (JWT) bootstraps or rotates a key FOR the service account;
    * the service principal itself (its own ``mk_`` credential — service rows
      have no browser session, so this is the "mints its own keys" path),
      which may mint only for ITSELF: a key never mints keys for someone
      else's principal.

    Either way ``org_api_keys.account_id`` is the service account, the role
    never exceeds the target's LIVE org role (≤ operator by construction), a
    grant-set (optional) flows through the live registry exclusion flag, and
    the 365-day service TTL is enforced at the ``create_api_key`` choke point
    for every surface.
    """
    if req.scope is not None and req.scope != "org":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="scope is immutable: service-account keys are org-scoped and cannot carry another scope",
        )
    name = _normalise_name(req.name)
    if req.role not in _KEY_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="role must be 'operator' or 'runner'. admin keys are prohibited.",
        )

    # Who may mint: a service principal may only mint for itself; minting for
    # ANOTHER principal (the admin bootstrap path) is an admin action.
    if principal.via_api_key:
        if principal.account_id != service_account_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="An API key may only mint keys for its own principal",
            )
    else:
        _require_admin(principal, "mint service-account API keys")

    expires_at = _parse_future_expires_at(req.expires_at)
    if req.grants is not None:
        await _resolve_grants_flag(principal.organisation_id)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            account, _membership = await _load_service_account(session, service_account_id, principal.organisation_id)
            _assert_not_decommissioned(account)

            # Cap the minted role at the TARGET's live org role (ADR 047
            # live-role re-read): a decommissioned / inactive service account
            # resolves None and is refused outright, and no key can ever
            # exceed what the principal itself holds.
            live_role = await resolve_role_from_membership(
                session,
                str(account.id),
                str(principal.organisation_id),
            )
            if live_role is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=_MSG_ACTIVE_MEMBERSHIP_REQUIRED,
                )
            if org_role_level(req.role) > org_role_level(live_role):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"Cannot use role '{req.role}' for an API key while the "
                        f"service account's live role is '{live_role}'"
                    ),
                )
            if req.grants is not None:
                await enforce_grants_mint_cap_for(
                    session,
                    account.id,
                    principal.organisation_id,
                    req.grants,
                )
            key, full_key = await create_api_key(
                session,
                org_id=principal.organisation_id,
                name=name,
                role=req.role,
                account_id=account.id,
                team_id=None,
                expires_at=expires_at,
                scope="org",
                grants=req.grants,
            )
    except ApiKeyTtlError as exc:
        # FAR-1478 / ADR 058: >365d on a service-owned key — client error,
        # never a silent clamp to 365d.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None

    await append_audit_event_isolated(
        session,
        principal,
        resource_type="api_key",
        event_type="api_key_created",
        resource_id=key.id,
        payload={
            "name": name,
            "role": req.role,
            "team_id": None,
            "auth_type": "api_key" if principal.via_api_key else "jwt",
            "key_scope": "org",
            "lookup_prefix": f"mk_{key.lookup_prefix}****",
            "owner_account_id": str(account.id),
            **({"grants": sorted(req.grants)} if req.grants is not None else {}),
        },
        log_key=_MSG_AUDIT_KEY_FAILED,
    )
    return ServiceAccountKeyResponse(
        id=key.id,
        name=key.name,
        role=key.role,
        key_value=full_key,
        lookup_prefix=f"mk_{key.lookup_prefix}****",
        created_at=key.created_at,
        owner_account_id=account.id,
        scope="org",
        grants=sorted(req.grants) if req.grants is not None else None,
    )
