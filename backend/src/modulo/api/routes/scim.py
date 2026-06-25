"""SCIM 2.0 provisioning endpoints.

Requires MODULO_SCIM_TOKEN env var for auth and MODULO_LICENSE_KEY for
enterprise gating. Maps SCIM Users → internal User, SCIM Groups → internal
Team + TeamMembership.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
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
from modulo.db.models.team import Team
from modulo.db.models.user import User
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

router = APIRouter(prefix="/scim/v2", tags=["scim"])

_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_SCIM_PATCH_OP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


# ── Helpers ──────────────────────────────────────────────────────────


def _license_gate(settings: Settings) -> None:
    if not settings.modulo_license_key:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="SCIM provisioning requires an enterprise license",
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


def _user_to_scim(user: User, base_url: str) -> dict[str, object]:
    given_name = (user.display_name or "").split(" ")[0] if user.display_name else ""
    parts = (user.display_name or "").split(" ")
    family_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {
        "schemas": [_SCIM_USER_SCHEMA],
        "id": str(user.id),
        "externalId": str(user.id),
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else "",
            "lastModified": user.updated_at.isoformat() if user.updated_at else "",
            "location": f"{base_url}/scim/v2/Users/{user.id}",
        },
        "userName": user.email,
        "name": {
            "formatted": user.display_name,
            "givenName": given_name,
            "familyName": family_name,
        },
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "active": user.active,
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
    return settings.modulo_public_url.rstrip("/")


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


# ── Enterprise license gate ──────────────────────────────────────────


def _require_enterprise(
    settings: Settings = Depends(get_settings),
) -> Settings:
    _license_gate(settings)
    return settings


# ── ServiceProviderConfig ────────────────────────────────────────────


@router.get("/ServiceProviderConfig")
async def get_service_provider_config(
    settings: Settings = Depends(_require_enterprise),
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
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ScimListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        users, total = await scim_list_users(
            session,
            principal.organisation_id,
            filter_str=filter,
            start_index=startIndex,
            count=count,
        )

    base_url = _get_base_url(settings)
    return ScimListResponse(
        schemas=[_SCIM_LIST_SCHEMA],
        totalResults=total,
        itemsPerPage=count,
        startIndex=startIndex,
        Resources=[_user_to_scim(u, base_url) for u in users],
    )


@router.post("/Users", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: ScimUserRequest,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)

        from modulo.db.crud.user import get_user_by_email

        existing = await get_user_by_email(session, body.userName)
        if existing is not None:
            raise _scim_error(
                status.HTTP_409_CONFLICT,
                f"User with userName {body.userName} already exists",
            )

        display_name = body.userName
        if body.name and body.name.formatted:
            display_name = body.name.formatted
        elif body.name and (body.name.givenName or body.name.familyName):
            parts = [p for p in (body.name.givenName, body.name.familyName) if p]
            display_name = " ".join(parts)

        user = await scim_create_user(
            session,
            org_id=principal.organisation_id,
            email=body.userName,
            display_name=display_name,
            active=body.active,
        )

    return _user_to_scim(user, _get_base_url(settings))


@router.get("/Users/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        user = await scim_get_user(session, principal.organisation_id, user_id)

    if user is None:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

    return _user_to_scim(user, _get_base_url(settings))


@router.put("/Users/{user_id}")
async def replace_user(
    user_id: uuid.UUID,
    body: ScimUserRequest,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        user = await scim_get_user(session, principal.organisation_id, user_id)
        if user is None:
            raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

        display_name = body.name.formatted if body.name and body.name.formatted else body.userName
        user = await scim_update_user(
            session,
            user,
            email=body.userName,
            display_name=display_name,
            active=body.active,
        )

    return _user_to_scim(user, _get_base_url(settings))


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: uuid.UUID,
    body: ScimPatchRequest,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        user = await scim_get_user(session, principal.organisation_id, user_id)
        if user is None:
            raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")

        for op in body.Operations:
            if op.op == "replace":
                if isinstance(op.value, dict):
                    if "userName" in op.value:
                        user.email = str(op.value["userName"])
                    if "active" in op.value:
                        user.active = bool(op.value["active"])
                    if isinstance(op.value.get("name"), dict):
                        name_data = op.value["name"]
                        formatted = name_data.get("formatted") or name_data.get("givenName", "") + " " + name_data.get(
                            "familyName", ""
                        )
                        user.display_name = str(formatted).strip()
                if op.path == "active":
                    user.active = bool(op.value)
            elif op.op == "remove":
                if op.path == "active":
                    user.active = False
            elif op.op == "add":
                if isinstance(op.value, dict):
                    if "userName" in op.value:
                        user.email = str(op.value["userName"])
                    if "active" in op.value:
                        user.active = bool(op.value["active"])

        await session.flush()

    return _user_to_scim(user, _get_base_url(settings))


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(_require_enterprise),
) -> None:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        deleted = await scim_delete_user_by_id(session, principal.organisation_id, user_id)

    if not deleted:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")


# ── Groups ───────────────────────────────────────────────────────────


@router.get("/Groups", response_model=ScimListResponse)
async def list_groups(
    filter: str | None = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(20, ge=1, le=100),
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ScimListResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        groups, total = await scim_list_groups(
            session,
            principal.organisation_id,
            filter_str=filter,
            start_index=startIndex,
            count=count,
        )

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
    body: ScimGroupRequest,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    _license_gate(settings)

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)

        from modulo.db.crud.team import get_team_by_name

        existing = await get_team_by_name(session, principal.organisation_id, body.displayName)
        if existing is not None:
            raise _scim_error(
                status.HTTP_409_CONFLICT,
                f"Group with displayName {body.displayName} already exists",
            )

        # Use the first member's ID as created_by, or a fallback.
        # SCIM does not carry a "creator" concept, so we use the first
        # admin-like user or a placeholder.
        from modulo.db.crud.user import list_users_for_org

        org_users = await list_users_for_org(session, principal.organisation_id)
        creator_id = org_users[0].id if org_users else None

        team = await scim_create_group(
            session,
            org_id=principal.organisation_id,
            display_name=body.displayName,
            created_by=creator_id,
        )

        for member_ref in body.members:
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

    base_url = _get_base_url(settings)
    members = [
        {
            "value": str(m.value),
            "$ref": f"{base_url}/scim/v2/Users/{m.value}",
            "type": "User",
        }
        for m in body.members
    ]
    return _group_to_scim(team, members, base_url)


@router.get("/Groups/{group_id}")
async def get_group(
    group_id: uuid.UUID,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        group = await scim_get_group(session, group_id)

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
    body: ScimGroupRequest,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        group = await scim_get_group(session, group_id)
        if group is None:
            raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")

        await scim_update_group(session, group, name=body.displayName)

        # Replace all members: remove existing, add new
        existing_members = await scim_list_group_members(session, group.id)
        for em in existing_members:
            await scim_remove_group_member(session, group.id, em.user_id)

        for member_ref in body.members:
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

    base_url = _get_base_url(settings)
    members = [
        {
            "value": str(m.value),
            "$ref": f"{base_url}/scim/v2/Users/{m.value}",
            "type": "User",
        }
        for m in body.members
    ]
    return _group_to_scim(group, members, base_url)


@router.patch("/Groups/{group_id}")
async def patch_group(
    group_id: uuid.UUID,
    body: ScimPatchRequest,
    settings: Settings = Depends(_require_enterprise),
    principal: ScimPrincipal = Depends(get_scim_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        group = await scim_get_group(session, group_id)
        if group is None:
            raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")

        for op in body.Operations:
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
    settings: Settings = Depends(_require_enterprise),
) -> None:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        deleted = await scim_delete_group_by_id(session, group_id)

    if not deleted:
        raise _scim_error(status.HTTP_404_NOT_FOUND, f"Group {group_id} not found")
