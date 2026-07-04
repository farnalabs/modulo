"""SCIM 2.0 provisioning endpoints.

Requires MODULO_SCIM_TOKEN env var for auth and MODULO_LICENSE_KEY for
Team gating. Maps SCIM Users → internal User, SCIM Groups → internal
Team + TeamMembership.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.scim_auth import ScimPrincipal, get_scim_principal
from modulo.db.crud.scim import (
    scim_add_group_member,
    scim_create_group,
    scim_create_user,
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

router = APIRouter(prefix="/scim/v2", tags=["scim"])

_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_SCIM_PATCH_OP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

_log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


def _license_gate(settings: Settings) -> None:
    if not settings.modulo_license_key:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="SCIM provisioning requires a Team license",
        )


def _scim_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "schemas": [_SCIM_ERROR_SCHEMA],
            "detail": detail,
            "status": str(status_code),
        },
    )


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
        return "http://localhost:8000"
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
    emails: list[ScimEmail] = []
    active: bool = True
    externalId: str | None = None


class ScimGroupRequest(BaseModel):
    schemas: list[str]
    displayName: str
    members: list[ScimMemberRef] = []
    externalId: str | None = None


class ScimPatchOperation(BaseModel):
    op: str
    path: str | None = None
    value: Any = None


class ScimPatchRequest(BaseModel):
    schemas: list[str]
    Operations: list[ScimPatchOperation] = []


class ScimListResponse(BaseModel):
    schemas: list[str]
    totalResults: int
    itemsPerPage: int
    startIndex: int
    Resources: list[dict[str, object]]


# ── Team license gate ──────────────────────────────────────────


def _require_team(
    settings: Settings = Depends(get_settings),
) -> Settings:
    _license_gate(settings)
    return settings


# ── ServiceProviderConfig ────────────────────────────────────────────


@router.get("/ServiceProviderConfig")
async def get_service_provider_config(
    settings: Settings = Depends(_require_team),
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


@router.get("/Users", response_model=ScimListResponse)
async def list_users(
    filter: str | None = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(20, ge=1, le=100),
    settings: Settings = Depends(_require_team),
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
                start_index=startIndex,
                count=count,
            )
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    base_url = _get_base_url(settings)
    return ScimListResponse(
        schemas=[_SCIM_LIST_SCHEMA],
        totalResults=total,
        itemsPerPage=count,
        startIndex=startIndex,
        Resources=[_user_to_scim(a, base_url) for a in accounts],
    )


@router.post("/Users", status_code=status.HTTP_201_CREATED)
async def create_user(
    req: ScimUserRequest,
    settings: Settings = Depends(_require_team),
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
                if membership is not None:
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
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    return _user_to_scim(account, _get_base_url(settings))


@router.get("/Users/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    settings: Settings = Depends(_require_team),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            account = await scim_get_user(session, principal.organisation_id, user_id)
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    if account is None:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

    return _user_to_scim(account, _get_base_url(settings))


@router.put("/Users/{user_id}")
async def replace_user(
    user_id: uuid.UUID,
    req: ScimUserRequest,
    settings: Settings = Depends(_require_team),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            account = await scim_get_user(session, principal.organisation_id, user_id)
            if account is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

            display_name = req.name.formatted if req.name and req.name.formatted else req.userName
            account = await scim_update_user(
                session,
                account,
                email=req.userName,
                display_name=display_name,
                active=req.active,
            )
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    return _user_to_scim(account, _get_base_url(settings))


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: uuid.UUID,
    req: ScimPatchRequest,
    settings: Settings = Depends(_require_team),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            account = await scim_get_user(session, principal.organisation_id, user_id)
            if account is None:
                raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

            for op in req.Operations:
                if op.op not in ("replace", "remove", "add"):
                    raise _scim_error(
                        status.HTTP_400_BAD_REQUEST,
                        f"Unsupported PATCH operation '{op.op}'. Supported: replace, remove, add",
                    )
                if op.op == "replace":
                    if isinstance(op.value, dict):
                        if "userName" in op.value:
                            account.email = str(op.value["userName"])
                        if "active" in op.value:
                            account.active = bool(op.value["active"])
                        if isinstance(op.value.get("name"), dict):
                            name_data = op.value["name"]
                            given = name_data.get("givenName") or ""
                            family = name_data.get("familyName") or ""
                            formatted = name_data.get("formatted") or (given + " " + family).strip()
                            account.display_name = str(formatted).strip()
                    if op.path == "active":
                        account.active = bool(op.value)
                elif op.op == "remove":
                    if op.path == "active":
                        account.active = False
                elif op.op == "add":
                    if isinstance(op.value, dict):
                        if "userName" in op.value:
                            account.email = str(op.value["userName"])
                        if "active" in op.value:
                            account.active = bool(op.value["active"])

            await session.flush()
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    return _user_to_scim(account, _get_base_url(settings))


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(_require_team),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await scim_delete_user_by_id(session, principal.organisation_id, user_id)
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    if not deleted:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")


# ── Groups ───────────────────────────────────────────────────────────


@router.get("/Groups", response_model=ScimListResponse)
async def list_groups(
    filter: str | None = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(20, ge=1, le=100),
    settings: Settings = Depends(_require_team),
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
                start_index=startIndex,
                count=count,
            )
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    base_url = _get_base_url(settings)
    resources: list[dict[str, object]] = []
    for g in groups:
        memberships = await scim_list_group_members(session, g.id)
        members = [
            {
                "value": str(m.user_id),
                "$ref": f"{base_url}/scim/v2/Users/{m.user_id}",
                "type": "User",
            }
            for m in memberships
        ]
        resources.append(_group_to_scim(g, members, base_url))

    return ScimListResponse(
        schemas=[_SCIM_LIST_SCHEMA],
        totalResults=total,
        itemsPerPage=count,
        startIndex=startIndex,
        Resources=resources,
    )


@router.post("/Groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    req: ScimGroupRequest,
    settings: Settings = Depends(_require_team),
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
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
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


@router.get("/Groups/{group_id}")
async def get_group(
    group_id: uuid.UUID,
    settings: Settings = Depends(_require_team),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await scim_get_group(session, group_id)
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    if group is None:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")

    base_url = _get_base_url(settings)
    memberships = await scim_list_group_members(session, group_id)
    members = [
        {
            "value": str(m.user_id),
            "$ref": f"{base_url}/scim/v2/Users/{m.user_id}",
            "type": "User",
        }
        for m in memberships
    ]
    return _group_to_scim(group, members, base_url)


@router.put("/Groups/{group_id}")
async def replace_group(
    group_id: uuid.UUID,
    req: ScimGroupRequest,
    settings: Settings = Depends(_require_team),
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
                await scim_remove_group_member(session, group.id, em.user_id)

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
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
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


@router.patch("/Groups/{group_id}")
async def patch_group(
    group_id: uuid.UUID,
    req: ScimPatchRequest,
    settings: Settings = Depends(_require_team),
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
                    if isinstance(op.value, dict):
                        if "displayName" in op.value:
                            await scim_update_group(session, group, name=str(op.value["displayName"]))
                        if "members" in op.value and isinstance(op.value["members"], list):
                            existing = await scim_list_group_members(session, group.id)
                            for em in existing:
                                await scim_remove_group_member(session, group.id, em.user_id)
                            for m in op.value["members"]:
                                if isinstance(m, dict) and "value" in m:
                                    try:
                                        uid = uuid.UUID(str(m["value"]))
                                    except ValueError:
                                        continue
                                    await scim_add_group_member(
                                        session,
                                        org_id=principal.organisation_id,
                                        team_id=group.id,
                                        user_id=uid,
                                    )
                elif op.op == "add":
                    if op.path == "members" or op.path is None:
                        values = op.value
                        if isinstance(values, dict):
                            values = [values]
                        if isinstance(values, list):
                            for m in values:
                                if isinstance(m, dict) and "value" in m:
                                    try:
                                        uid = uuid.UUID(str(m["value"]))
                                    except ValueError:
                                        continue
                                    await scim_add_group_member(
                                        session,
                                        org_id=principal.organisation_id,
                                        team_id=group.id,
                                        user_id=uid,
                                    )
                elif op.op == "remove":
                    if op.path and op.path.startswith("members"):
                        # Extract user ID from path: members[value eq "uuid"]
                        import re as _re

                        m = _re.search(r'value\s+eq\s+"([^"]+)"', op.path)
                        if m:
                            uid_str = m.group(1)
                            try:
                                uid = uuid.UUID(uid_str)
                            except ValueError:
                                continue
                            await scim_remove_group_member(session, group.id, uid)
                    elif op.value:
                        if isinstance(op.value, dict) and "value" in op.value:
                            try:
                                uid = uuid.UUID(str(op.value["value"]))
                            except ValueError:
                                continue
                            await scim_remove_group_member(session, group.id, uid)
                        elif isinstance(op.value, list):
                            for item in op.value:
                                if isinstance(item, dict) and "value" in item:
                                    try:
                                        uid = uuid.UUID(str(item["value"]))
                                    except ValueError:
                                        continue
                                    await scim_remove_group_member(session, group.id, uid)
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    base_url = _get_base_url(settings)
    memberships = await scim_list_group_members(session, group.id)
    members = [
        {
            "value": str(m.user_id),
            "$ref": f"{base_url}/scim/v2/Users/{m.user_id}",
            "type": "User",
        }
        for m in memberships
    ]
    return _group_to_scim(group, members, base_url)


@router.delete("/Groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(_require_team),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await scim_delete_group_by_id(session, group_id)
    except ProgrammingError:
        _log.warning("SCIM endpoint failed: database migration required")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM provisioning is not available. Run database migrations to enable it.",
        ) from None

    if not deleted:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")
