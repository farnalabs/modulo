"""SCIM 2.0 provisioning endpoints.

Requires MODULO_SCIM_TOKEN env var for auth. Gated behind the "scim"
feature flag (require_feature). Maps SCIM Users → internal User, SCIM
Groups → internal Team + TeamMembership.

ADR 017: SCIM is EXEMPT from the org-role sweep at phase 1 — every route
authenticates via the shared-secret ``MODULO_SCIM_TOKEN`` (an enumerated
channel with a dedicated non-role auth mechanism). No SCIM route passes
``org_role`` to the CRUD layer: ``scim_create_user`` defaults to the
``runner`` grant and ``scim_update_user`` has a *functional* role-UPDATE
that is intentionally left unwired. Do not wire a role-update path without
an ADR 017 follow-up (Phase 3 documents the runner-default grant).
"""

import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.dependencies import get_db_session
from modulo.api.routes.admin import _raise_bg_pgcode
from modulo.auth.scim_auth import ScimPrincipal, get_scim_principal, require_scim_feature
from modulo.db.crud.last_admin_guard import (
    LastAdminLockoutError,
    LastAdminLockoutUnavailableError,
    assert_not_last_admin,
)
from modulo.db.crud.scim import (
    scim_add_group_member,
    scim_create_group,
    scim_create_user,
    scim_deactivate_user,
    scim_delete_group_by_id,
    scim_delete_user_by_id,
    scim_get_group,
    scim_get_user,
    scim_list_group_members,
    scim_list_groups,
    scim_list_users,
    scim_remove_group_member,
    scim_update_group,
    scim_update_user,
)
from modulo.db.models.account import Account
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_CODE_SCIM_LIST_USERS = "scim.list_users"
_MSG_SCIM_ENDPOINT_FAILED_DATABASE = "SCIM endpoint failed: database migration required"
_MSG_SCIM_PROVISIONING_NOT_AVAILABLE = "SCIM provisioning is not available. Run database migrations to enable it."
_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE = "SCIM provisioning is temporarily unavailable due to a database error"
_CODE_SCIM_CREATE_USER = "scim.create_user"
_CODE_SCIM_GET_USER = "scim.get_user"
_MSG_NO_ACTIVE_ADMIN_EXISTS = "No active admin exists in this org; provision a replacement admin first"
_MSG_COULD_NOT_VERIFY_LAST = "Could not verify the last-admin invariant. Please try again."
_CODE_SCIM_REPLACE_USER = "scim.replace_user"
_CODE_SCIM_PATCH_USER = "scim.patch_user"
_CODE_SCIM_DELETE_USER = "scim.delete_user"
_CODE_SCIM_LIST_GROUPS = "scim.list_groups"
_CODE_SCIM_CREATE_GROUP = "scim.create_group"
_CODE_SCIM_GET_GROUP = "scim.get_group"
_CODE_SCIM_REPLACE_GROUP = "scim.replace_group"
_CODE_SCIM_PATCH_GROUP = "scim.patch_group"
_CODE_SCIM_DELETE_GROUP = "scim.delete_group"


router = APIRouter(prefix="/scim/v2", tags=["scim"])

_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


# ── Helpers ──────────────────────────────────────────────────────────


_log = logging.getLogger(__name__)


def _scim_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "schemas": [_SCIM_ERROR_SCHEMA],
            "detail": detail,
            "status": str(status_code),
        },
    )


_SCIM_ADMIN_CALLER_SQL = text(
    "SELECT a.id FROM org_memberships om JOIN accounts a ON a.id = om.account_id "
    "WHERE om.organisation_id = :org AND om.role = 'admin' AND om.deactivated_at IS NULL "
    "AND a.active IS TRUE AND a.is_break_glass IS FALSE ORDER BY om.joined_at, a.id LIMIT 1"
)


async def _resolve_scim_admin_caller(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID | None:
    """Deterministically resolve the SCIM caller for a deactivation.

    SCIM authenticates via the shared MODULO_SCIM_TOKEN (no per-user identity),
    so the caller is resolved to the org's first active non-break-glass admin
    (deterministic incl. the ``joined_at, a.id`` tiebreaker). Break-glass
    accounts are excluded; if none exists the deactivation is rejected 409.
    """
    result = await session.execute(_SCIM_ADMIN_CALLER_SQL, {"org": org_id})
    row = result.first()
    return row[0] if row is not None else None


async def _deactivate_scim_user(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Account:
    """Deactivate a SCIM user, resolving the caller and enforcing last-admin.

    Raises ``_scim_error`` on conflict or not-found, mirroring the route-level
    behaviour so callers (replace/patch/delete) share one implementation.
    """
    caller = await _resolve_scim_admin_caller(session, org_id)
    if caller is None:
        raise _scim_error(
            status.HTTP_409_CONFLICT,
            _MSG_NO_ACTIVE_ADMIN_EXISTS,
        )
    await assert_not_last_admin(
        session,
        org_id=org_id,
        target_account_id=user_id,
        target_role_after=None,
        target_active_after=False,
    )
    account = await scim_deactivate_user(
        session,
        org_id,
        user_id,
        caller_account_id=caller,
    )
    if account is None:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")
    return account


def _user_to_scim(account: Account, base_url: str) -> dict[str, object]:
    given_name = (account.display_name or "").split(" ")[0] if account.display_name else ""
    parts = (account.display_name or "").split(" ")
    family_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {
        "schemas": [_SCIM_USER_SCHEMA],
        "id": str(account.id),
        "externalId": str(account.id),
        "meta": {
            "resourceType": "User",
            "created": account.created_at.isoformat() if account.created_at else "",
            "lastModified": account.updated_at.isoformat() if account.updated_at else "",
            "location": f"{base_url}/scim/v2/Users/{account.id}",
        },
        "userName": account.email,
        "name": {
            "formatted": account.display_name,
            "givenName": given_name,
            "familyName": family_name,
        },
        "emails": [{"value": account.email, "type": "work", "primary": True}],
        "active": account.active,
    }


def _group_to_scim(group: Team, members: list[dict[str, str]], base_url: str) -> dict[str, object]:
    return {
        "schemas": [_SCIM_GROUP_SCHEMA],
        "id": str(group.id),
        "externalId": str(group.id),
        "meta": {
            "resourceType": "Group",
            "created": group.created_at.isoformat() if group.created_at else "",
            "lastModified": group.updated_at.isoformat() if group.updated_at else "",
            "location": f"{base_url}/scim/v2/Groups/{group.id}",
        },
        "displayName": group.name,
        "members": members,
    }


def _get_base_url(settings: Settings) -> str:
    url = settings.modulo_public_url
    if not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM cannot resolve base URL: MODULO_PUBLIC_URL is not configured",
        )
    return url.rstrip("/")


# ── Request / Response models ────────────────────────────────────────


class ScimName(BaseModel):
    formatted: str | None = None
    givenName: str | None = None
    familyName: str | None = None


class ScimEmail(BaseModel):
    value: str
    type: str = "work"
    primary: bool = False


class ScimMemberRef(BaseModel):
    value: str
    type: str = "User"
    ref: str | None = Field(None, alias="$ref")


class ScimUserRequest(BaseModel):
    schemas: list[str]
    userName: str
    name: ScimName | None = None
    emails: list[ScimEmail] = Field(default_factory=list)
    active: bool = True
    externalId: str | None = None


class ScimGroupRequest(BaseModel):
    schemas: list[str]
    displayName: str
    members: list[ScimMemberRef] = Field(default_factory=list)
    externalId: str | None = None


class ScimPatchOperation(BaseModel):
    op: str
    path: str | None = None
    value: Any = None


class ScimPatchRequest(BaseModel):
    schemas: list[str]
    Operations: list[ScimPatchOperation] = Field(default_factory=list)


class ScimListResponse(BaseModel):
    schemas: list[str]
    totalResults: int
    itemsPerPage: int
    startIndex: int
    Resources: list[dict[str, object]]


# ── ServiceProviderConfig ────────────────────────────────────────────


@router.get("/ServiceProviderConfig", dependencies=[Depends(require_scim_feature)])
async def get_service_provider_config(
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
) -> dict[str, object]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 100},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "name": "Bearer Token",
                "description": "Bearer token from MODULO_SCIM_TOKEN env var",
                "specUri": "https://www.rfc-editor.org/rfc/rfc6750",
                "type": "bearer",
                "primary": True,
            }
        ],
    }


# ── Users ────────────────────────────────────────────────────────────


@router.get("/Users", dependencies=[Depends(require_scim_feature)])
async def list_users(
    filter: str | None = Query(None),
    start_index: int = Query(1, ge=1, alias="startIndex"),
    count: int = Query(20, ge=1, le=100),
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ScimListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            accounts, total = await scim_list_users(
                session,
                principal.organisation_id,
                filter_str=filter,
                start_index=start_index,
                count=count,
            )
    except HTTPException:
        _log.warning("SCIM list_users: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_LIST_USERS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_LIST_USERS)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_LIST_USERS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM list_users failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM list_users failed due to an unexpected error",
        ) from None

    base_url = _get_base_url(settings)
    return ScimListResponse(
        schemas=[_SCIM_LIST_SCHEMA],
        totalResults=total,
        itemsPerPage=count,
        startIndex=start_index,
        Resources=[_user_to_scim(a, base_url) for a in accounts],
    )


@router.post("/Users", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_scim_feature)])
async def create_user(
    req: ScimUserRequest,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            from modulo.db.crud.account import get_account_by_email

            existing = await get_account_by_email(session, req.userName)
            if existing is not None:
                from modulo.db.crud.org_membership import get_membership_by_account_and_org

                membership = await get_membership_by_account_and_org(session, existing.id, principal.organisation_id)
                if membership is not None and membership.deactivated_at is None:
                    raise _scim_error(
                        status.HTTP_409_CONFLICT,
                        f"User with userName {req.userName} already exists in this org",
                    )

            display_name = req.userName
            if req.name and req.name.formatted:
                display_name = req.name.formatted
            elif req.name and (req.name.givenName or req.name.familyName):
                parts = [p for p in (req.name.givenName, req.name.familyName) if p]
                display_name = " ".join(parts)

            account = await scim_create_user(
                session,
                org_id=principal.organisation_id,
                email=req.userName,
                display_name=display_name,
                active=req.active,
            )
    except HTTPException:
        _log.warning("SCIM create_user: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_CREATE_USER)
        _log.warning("SCIM create_user: duplicate key violation")
        raise _scim_error(
            status.HTTP_409_CONFLICT,
            f"User with userName {req.userName} already exists",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_CREATE_USER)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_CREATE_USER)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM create_user failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM create_user failed due to an unexpected error",
        ) from None

    return _user_to_scim(account, _get_base_url(settings))


@router.get("/Users/{user_id}", dependencies=[Depends(require_scim_feature)])
async def get_user(
    user_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            account = await scim_get_user(session, principal.organisation_id, user_id)
    except HTTPException:
        _log.warning("SCIM get_user: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_GET_USER)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_GET_USER)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_GET_USER)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM get_user failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM get_user failed due to an unexpected error",
        ) from None

    if account is None:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

    return _user_to_scim(account, _get_base_url(settings))


@router.put("/Users/{user_id}", dependencies=[Depends(require_scim_feature)])
async def replace_user(
    user_id: uuid.UUID,
    req: ScimUserRequest,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            account = await scim_get_user(session, principal.organisation_id, user_id)
            if account is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

            if not req.active:
                account = await _deactivate_scim_user(session, principal.organisation_id, user_id)
            else:
                display_name = req.name.formatted if req.name and req.name.formatted else req.userName
                account = await scim_update_user(
                    session,
                    account,
                    org_id=principal.organisation_id,
                    email=req.userName,
                    display_name=display_name,
                    active=req.active,
                )
    except LastAdminLockoutError as exc:
        raise _scim_error(status.HTTP_409_CONFLICT, exc.reason) from None
    except LastAdminLockoutUnavailableError:
        _log.exception("scim.replace_user.last_admin_guard_unavailable")
        raise _scim_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _MSG_COULD_NOT_VERIFY_LAST,
        ) from None
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_REPLACE_USER)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_REPLACE_USER)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        _log.exception(_CODE_SCIM_REPLACE_USER)
        _raise_bg_pgcode(
            exc,
            unauthorized_status=status.HTTP_409_CONFLICT,
            conflict_status=status.HTTP_409_CONFLICT,
            not_found_status=status.HTTP_404_NOT_FOUND,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM replace_user failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM replace_user failed due to an unexpected error",
        ) from None

    return _user_to_scim(account, _get_base_url(settings))


def _apply_user_replace_op(account: Account, op: ScimPatchOperation) -> bool:
    """Apply a ``replace`` PATCH operation to a user; True when deactivation requested."""
    deactivate_requested = False
    if isinstance(op.value, dict):
        if "userName" in op.value:
            account.email = str(op.value["userName"])
        if "active" in op.value:
            account.active = bool(op.value["active"])
            deactivate_requested = not bool(op.value["active"])
        name_data = op.value.get("name")
        if isinstance(name_data, dict):
            given = name_data.get("givenName") or ""
            family = name_data.get("familyName") or ""
            formatted = name_data.get("formatted") or (given + " " + family).strip()
            account.display_name = str(formatted).strip()
    if op.path == "active":
        account.active = bool(op.value)
        deactivate_requested = deactivate_requested or not bool(op.value)
    return deactivate_requested


def _apply_user_remove_op(account: Account, op: ScimPatchOperation) -> bool:
    """Apply a ``remove`` PATCH operation to a user; True when deactivation requested."""
    if op.path == "active":
        account.active = False
        return True
    return False


def _apply_user_add_op(account: Account, op: ScimPatchOperation) -> bool:
    """Apply an ``add`` PATCH operation to a user; True when deactivation requested."""
    deactivate_requested = False
    if isinstance(op.value, dict) and "userName" in op.value:
        account.email = str(op.value["userName"])
        if "active" in op.value:
            account.active = bool(op.value["active"])
            deactivate_requested = not bool(op.value["active"])
    return deactivate_requested


def _apply_user_patch_ops(account: Account, operations: list[ScimPatchOperation]) -> bool:
    """Apply SCIM User PATCH operations to an account; True when deactivation requested."""
    deactivate_requested = False
    for op in operations:
        if op.op not in ("replace", "remove", "add"):
            raise _scim_error(
                status.HTTP_400_BAD_REQUEST,
                f"Unsupported PATCH operation '{op.op}'. Supported: replace, remove, add",
            )
        if op.op == "replace":
            op_deactivate = _apply_user_replace_op(account, op)
        elif op.op == "remove":
            op_deactivate = _apply_user_remove_op(account, op)
        else:
            op_deactivate = _apply_user_add_op(account, op)
        deactivate_requested = op_deactivate or deactivate_requested
    return deactivate_requested


@router.patch("/Users/{user_id}", dependencies=[Depends(require_scim_feature)])
async def patch_user(
    user_id: uuid.UUID,
    req: ScimPatchRequest,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            account = await scim_get_user(session, principal.organisation_id, user_id)
            if account is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

            deactivate_requested = _apply_user_patch_ops(account, req.Operations)
            if deactivate_requested:
                account = await _deactivate_scim_user(session, principal.organisation_id, user_id)
            else:
                await session.flush()
    except LastAdminLockoutError as exc:
        raise _scim_error(status.HTTP_409_CONFLICT, exc.reason) from None
    except LastAdminLockoutUnavailableError:
        _log.exception("scim.patch_user.last_admin_guard_unavailable")
        raise _scim_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _MSG_COULD_NOT_VERIFY_LAST,
        ) from None
    except HTTPException:
        _log.warning("SCIM patch_user: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_PATCH_USER)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_PATCH_USER)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        _log.exception(_CODE_SCIM_PATCH_USER)
        _raise_bg_pgcode(
            exc,
            unauthorized_status=status.HTTP_409_CONFLICT,
            conflict_status=status.HTTP_409_CONFLICT,
            not_found_status=status.HTTP_404_NOT_FOUND,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM patch_user failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM patch_user failed due to an unexpected error",
        ) from None

    return _user_to_scim(account, _get_base_url(settings))


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_scim_feature)])
async def delete_user(
    user_id: uuid.UUID,
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            caller = await _resolve_scim_admin_caller(session, principal.organisation_id)
            if caller is None:
                raise _scim_error(
                    status.HTTP_409_CONFLICT,
                    _MSG_NO_ACTIVE_ADMIN_EXISTS,
                )
            account = await scim_get_user(session, principal.organisation_id, user_id)
            if account is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")
            await assert_not_last_admin(
                session,
                org_id=principal.organisation_id,
                target_account_id=user_id,
                target_role_after=None,
                target_active_after=False,
            )
            deleted = await scim_delete_user_by_id(
                session,
                principal.organisation_id,
                user_id,
                caller_account_id=caller,
            )
    except LastAdminLockoutError as exc:
        raise _scim_error(status.HTTP_409_CONFLICT, exc.reason) from None
    except LastAdminLockoutUnavailableError:
        _log.exception("scim.delete_user.last_admin_guard_unavailable")
        raise _scim_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _MSG_COULD_NOT_VERIFY_LAST,
        ) from None
    except HTTPException:
        _log.warning("SCIM delete_user: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_DELETE_USER)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_DELETE_USER)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        _log.exception(_CODE_SCIM_DELETE_USER)
        _raise_bg_pgcode(
            exc,
            unauthorized_status=status.HTTP_409_CONFLICT,
            conflict_status=status.HTTP_409_CONFLICT,
            not_found_status=status.HTTP_404_NOT_FOUND,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM delete_user failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM delete_user failed due to an unexpected error",
        ) from None

    if not deleted:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")


# ── Groups ───────────────────────────────────────────────────────────


@router.get("/Groups", dependencies=[Depends(require_scim_feature)])
async def list_groups(
    filter: str | None = Query(None),
    start_index: int = Query(1, ge=1, alias="startIndex"),
    count: int = Query(20, ge=1, le=100),
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ScimListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            groups, total = await scim_list_groups(
                session,
                principal.organisation_id,
                filter_str=filter,
                start_index=start_index,
                count=count,
            )
            base_url = _get_base_url(settings)
            resources: list[dict[str, object]] = []
            for g in groups:
                memberships = await scim_list_group_members(session, g.id)
                members = [
                    {
                        "value": str(m.account_id),
                        "$ref": f"{base_url}/scim/v2/Users/{m.account_id}",
                        "type": "User",
                    }
                    for m in memberships
                ]
                resources.append(_group_to_scim(g, members, base_url))
    except HTTPException:
        _log.warning("SCIM list_groups: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_LIST_GROUPS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_LIST_GROUPS)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_LIST_GROUPS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM list_groups failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM list_groups failed due to an unexpected error",
        ) from None

    return ScimListResponse(
        schemas=[_SCIM_LIST_SCHEMA],
        totalResults=total,
        itemsPerPage=count,
        startIndex=start_index,
        Resources=resources,
    )


@router.post("/Groups", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_scim_feature)])
async def create_group(
    req: ScimGroupRequest,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            from modulo.db.crud.team import get_team_by_name

            existing = await get_team_by_name(session, principal.organisation_id, req.displayName)
            if existing is not None:
                raise _scim_error(
                    status.HTTP_409_CONFLICT,
                    f"Group with displayName {req.displayName} already exists",
                )

            # Use the first member's ID as created_by, or a fallback.
            # SCIM does not carry a "creator" concept, so we use the first
            # admin-like account or a placeholder.
            from modulo.db.crud.account import get_account_by_id
            from modulo.db.crud.org_membership import list_memberships_for_org

            org_memberships = await list_memberships_for_org(session, principal.organisation_id)
            creator_id = None
            if org_memberships:
                first_account = await get_account_by_id(session, org_memberships[0].account_id)
                if first_account is not None:
                    creator_id = first_account.id

            team = await scim_create_group(
                session,
                org_id=principal.organisation_id,
                display_name=req.displayName,
                account_id=creator_id,
            )

            for member_ref in req.members:
                try:
                    uid = uuid.UUID(member_ref.value)
                except ValueError:
                    continue
                user = await scim_get_user(session, principal.organisation_id, uid)
                if user is not None:
                    await scim_add_group_member(
                        session,
                        org_id=principal.organisation_id,
                        team_id=team.id,
                        user_id=uid,
                    )
    except HTTPException:
        _log.warning("SCIM create_group: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_CREATE_GROUP)
        _log.warning("SCIM create_group: duplicate key violation")
        raise _scim_error(
            status.HTTP_409_CONFLICT,
            f"Group with displayName {req.displayName} already exists",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_CREATE_GROUP)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_CREATE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM create_group failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM create_group failed due to an unexpected error",
        ) from None

    base_url = _get_base_url(settings)
    members = [
        {
            "value": str(m.value),
            "$ref": f"{base_url}/scim/v2/Users/{m.value}",
            "type": "User",
        }
        for m in req.members
    ]
    return _group_to_scim(team, members, base_url)


@router.get("/Groups/{group_id}", dependencies=[Depends(require_scim_feature)])
async def get_group(
    group_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await scim_get_group(session, group_id)
            if group is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")
            base_url = _get_base_url(settings)
            memberships = await scim_list_group_members(session, group_id)
            members = [
                {
                    "value": str(m.account_id),
                    "$ref": f"{base_url}/scim/v2/Users/{m.account_id}",
                    "type": "User",
                }
                for m in memberships
            ]
    except HTTPException:
        _log.warning("SCIM get_group: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_GET_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_GET_GROUP)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_GET_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM get_group failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM get_group failed due to an unexpected error",
        ) from None

    return _group_to_scim(group, members, base_url)


@router.put("/Groups/{group_id}", dependencies=[Depends(require_scim_feature)])
async def replace_group(
    group_id: uuid.UUID,
    req: ScimGroupRequest,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await scim_get_group(session, group_id)
            if group is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")

            await scim_update_group(session, group, name=req.displayName)

            # Replace all members: remove existing, add new
            existing_members = await scim_list_group_members(session, group.id)
            for em in existing_members:
                await scim_remove_group_member(session, group.id, em.account_id)

            for member_ref in req.members:
                try:
                    uid = uuid.UUID(member_ref.value)
                except ValueError:
                    continue
                user = await scim_get_user(session, principal.organisation_id, uid)
                if user is not None:
                    await scim_add_group_member(
                        session,
                        org_id=principal.organisation_id,
                        team_id=group.id,
                        user_id=uid,
                    )
    except HTTPException:
        _log.warning("SCIM replace_group: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_REPLACE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_REPLACE_GROUP)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_REPLACE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM replace_group failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM replace_group failed due to an unexpected error",
        ) from None

    base_url = _get_base_url(settings)
    members = [
        {
            "value": str(m.value),
            "$ref": f"{base_url}/scim/v2/Users/{m.value}",
            "type": "User",
        }
        for m in req.members
    ]
    return _group_to_scim(group, members, base_url)


def _parse_member_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def _replace_group_members(session: AsyncSession, group: Any, members_value: Any, org_id: uuid.UUID) -> None:
    existing = await scim_list_group_members(session, group.id)
    for em in existing:
        await scim_remove_group_member(session, group.id, em.account_id)
    for uid in _iter_member_uuids(members_value):
        await scim_add_group_member(
            session,
            org_id=org_id,
            team_id=group.id,
            user_id=uid,
        )


async def _apply_replace_group_op(session: AsyncSession, group: Any, op: Any, org_id: uuid.UUID) -> None:
    if not isinstance(op.value, dict):
        return
    if "displayName" in op.value:
        await scim_update_group(session, group, name=str(op.value["displayName"]))
    if "members" in op.value and isinstance(op.value["members"], list):
        await _replace_group_members(session, group, op.value["members"], org_id)


def _iter_member_uuids(values: Any) -> list[uuid.UUID]:
    if isinstance(values, dict):
        values = [values]
    result: list[uuid.UUID] = []
    if isinstance(values, list):
        for m in values:
            if isinstance(m, dict) and "value" in m:
                uid = _parse_member_uuid(m["value"])
                if uid is not None:
                    result.append(uid)
    return result


async def _apply_add_group_op(session: AsyncSession, group: Any, op: Any, org_id: uuid.UUID) -> None:
    if op.path == "members" or op.path is None:
        for uid in _iter_member_uuids(op.value):
            await scim_add_group_member(
                session,
                org_id=org_id,
                team_id=group.id,
                user_id=uid,
            )


async def _remove_member_by_path(session: AsyncSession, group: Any, path: str) -> None:
    # Extract user ID from path: members[value eq "uuid"]
    m = re.search(r'value\s+eq\s+"([^"]+)"', path)
    if m:
        uid = _parse_member_uuid(m.group(1))
        if uid is not None:
            await scim_remove_group_member(session, group.id, uid)


async def _remove_members_from_value(session: AsyncSession, group: Any, value: Any) -> None:
    if isinstance(value, dict) and "value" in value:
        uid = _parse_member_uuid(value["value"])
        if uid is not None:
            await scim_remove_group_member(session, group.id, uid)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "value" in item:
                uid = _parse_member_uuid(item["value"])
                if uid is not None:
                    await scim_remove_group_member(session, group.id, uid)


async def _apply_remove_group_op(session: AsyncSession, group: Any, op: Any) -> None:
    if op.path and op.path.startswith("members"):
        await _remove_member_by_path(session, group, op.path)
    elif op.value:
        await _remove_members_from_value(session, group, op.value)


async def _build_group_member_refs(session: AsyncSession, group: Any, base_url: str) -> list[dict[str, str]]:
    memberships = await scim_list_group_members(session, group.id)
    return [
        {
            "value": str(m.account_id),
            "$ref": f"{base_url}/scim/v2/Users/{m.account_id}",
            "type": "User",
        }
        for m in memberships
    ]


@router.patch("/Groups/{group_id}", dependencies=[Depends(require_scim_feature)])
async def patch_group(
    group_id: uuid.UUID,
    req: ScimPatchRequest,
    settings: Settings = Depends(get_settings),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await scim_get_group(session, group_id)
            if group is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")

            for op in req.Operations:
                if op.op not in ("replace", "remove", "add"):
                    raise _scim_error(
                        status.HTTP_400_BAD_REQUEST,
                        f"Unsupported PATCH operation '{op.op}'. Supported: replace, remove, add",
                    )
                if op.op == "replace":
                    await _apply_replace_group_op(session, group, op, principal.organisation_id)
                elif op.op == "add":
                    await _apply_add_group_op(session, group, op, principal.organisation_id)
                elif op.op == "remove":
                    await _apply_remove_group_op(session, group, op)
            base_url = _get_base_url(settings)
            members = await _build_group_member_refs(session, group, base_url)
    except HTTPException:
        _log.warning("SCIM patch_group: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_PATCH_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_PATCH_GROUP)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_PATCH_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM patch_group failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM patch_group failed due to an unexpected error",
        ) from None

    return _group_to_scim(group, members, base_url)


@router.delete(
    "/Groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_scim_feature)],
)
async def delete_group(
    group_id: uuid.UUID,
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await scim_delete_group_by_id(session, group_id)
    except HTTPException:
        _log.warning("SCIM delete_group: re-raising HTTPException")
        raise
    except IntegrityError:
        _log.exception(_CODE_SCIM_DELETE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_SCIM_DELETE_GROUP)
        _log.warning(_MSG_SCIM_ENDPOINT_FAILED_DATABASE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_SCIM_PROVISIONING_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_SCIM_DELETE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_SCIM_PROVISIONING_TEMPORARILY_UNAVAILABLE,
        ) from None
    except Exception:
        _log.exception("SCIM delete_group failed: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCIM delete_group failed due to an unexpected error",
        ) from None

    if not deleted:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")
