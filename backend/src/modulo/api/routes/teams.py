"""Team management REST routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_feature
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY, TEAM_ROLE_HIERARCHY
from modulo.db.crud.team import (
    create_team,
    delete_team,
    get_team,
    get_team_by_name,
    list_teams,
    update_team,
)
from modulo.db.crud.team_membership import (
    add_team_member,
    get_membership,
    get_membership_by_team_and_account,
    list_team_members,
    remove_team_member,
    update_member_role,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

router = APIRouter(
    prefix="/api/v1/teams",
    tags=["teams"],
    dependencies=[require_feature("team_rbac")],
)


class CreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_whitespace_name(cls, v: str) -> str:
        stripped = v.strip() if isinstance(v, str) else v
        if not stripped:
            raise ValueError("Team name must not be empty or whitespace-only")
        return stripped


class UpdateTeamRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_whitespace_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip() if isinstance(v, str) else v
        if not stripped:
            raise ValueError("Team name must not be empty or whitespace-only")
        return stripped


class TeamResponse(BaseModel):
    id: str
    name: str
    description: str | None
    account_id: str
    created_at: str


class TeamListResponse(BaseModel):
    items: list[TeamResponse]
    total: int
    page: int
    page_size: int


class AddMemberRequest(BaseModel):
    user_id: str = Field(min_length=36, max_length=36)
    role: str = Field(default="viewer", pattern=r"^(viewer|runner|operator)$")


class ChangeMemberRoleRequest(BaseModel):
    role: str = Field(pattern=r"^(viewer|runner|operator)$")


class MembershipResponse(BaseModel):
    id: str
    team_id: str
    user_id: str
    role: str
    created_at: str


class MembershipListResponse(BaseModel):
    items: list[MembershipResponse]
    total: int
    page: int
    page_size: int


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if ORG_ROLE_HIERARCHY.get(principal.org_role, -1) < ORG_ROLE_HIERARCHY["admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can perform this action",
        )


@router.get("", response_model=TeamListResponse)
async def list_teams_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TeamListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            result = await list_teams(session, org_id=current_user.organisation_id, page=page, page_size=page_size)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching teams.",
        ) from None

    return TeamListResponse(
        items=[
            TeamResponse(
                id=str(t.id),
                name=t.name,
                description=t.description,
                account_id=str(t.account_id),
                created_at=t.created_at.isoformat(),
            )
            for t in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team_endpoint(
    req: CreateTeamRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            existing = await get_team_by_name(session, current_user.organisation_id, req.name)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A team with this name already exists in your organisation",
                )
            team = await create_team(
                session,
                org_id=current_user.organisation_id,
                name=req.name,
                account_id=current_user.account_id,
                description=req.description,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat(),
    )


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team_endpoint(
    team_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            team = await get_team(session, team_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat(),
    )


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team_endpoint(
    team_id: uuid.UUID,
    req: UpdateTeamRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    _require_admin(current_user)

    updates = req.model_dump(exclude_unset=True)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            if "name" in updates:
                existing = await get_team_by_name(session, current_user.organisation_id, updates["name"])
                if existing is not None and existing.id != team_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A team with this name already exists in your organisation",
                    )

            team = await update_team(session, team_id, updates)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat(),
    )


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team_endpoint(
    team_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            from sqlalchemy import func, select

            from modulo.db.models.connector_instance import ConnectorInstance
            from modulo.db.models.model_backend import ModelBackend
            from modulo.db.models.pipeline import Pipeline
            from modulo.db.models.stage import Stage

            resource_checks: list[tuple[str, int]] = []
            for model_cls, label in [
                (Pipeline, "pipeline"),
                (Stage, "stage"),
                (ConnectorInstance, "connector"),
                (ModelBackend, "model backend"),
            ]:
                count = (
                    await session.execute(
                        select(func.count()).select_from(model_cls).where(model_cls.owner_team_id == team_id)
                    )
                ).scalar() or 0
                if count > 0:
                    resource_checks.append((label, count))

            if resource_checks:
                details = "; ".join(f"{count} {label}(s)" for label, count in resource_checks)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot delete team: still has resources — {details}",
                )

            deleted = await delete_team(session, team_id)

        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

        from modulo.core.audit_logger import append_audit_event

        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="team_deleted",
                actor_user_id=current_user.account_id,
                resource_type="team",
                resource_id=team_id,
                payload_json={"team_id": str(team_id)},
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.get("/{team_id}/members", response_model=MembershipListResponse)
async def list_members_endpoint(
    team_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            result = await list_team_members(session, team_id=team_id, page=page, page_size=page_size)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return MembershipListResponse(
        items=[
            MembershipResponse(
                id=str(m.id),
                team_id=str(m.team_id),
                user_id=str(m.user_id),
                role=m.role,
                created_at=m.created_at.isoformat(),
            )
            for m in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/{team_id}/members",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member_endpoint(
    team_id: uuid.UUID,
    req: AddMemberRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipResponse:
    user_id = uuid.UUID(req.user_id)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            from modulo.db.crud.account import get_account_by_id
            from modulo.db.crud.org_membership import get_membership_by_account_and_org

            team = await get_team(session, team_id)
            if team is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

            # Authorise: admin (all-powerful) OR team operator of this team
            is_admin = ORG_ROLE_HIERARCHY.get(current_user.org_role, -1) >= ORG_ROLE_HIERARCHY["admin"]
            if not is_admin:
                caller_membership = await get_membership_by_team_and_account(session, team_id, current_user.account_id)
                if caller_membership is None or caller_membership.role != "operator":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Only admin users or team operators can add members",
                    )
                if TEAM_ROLE_HIERARCHY.get(req.role, -1) > TEAM_ROLE_HIERARCHY.get(caller_membership.role, -1):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Cannot grant role '{req.role}' above your own team role '{caller_membership.role}'",
                    )

            target_account = await get_account_by_id(session, user_id)
            target_membership = await get_membership_by_account_and_org(session, user_id, current_user.organisation_id)
            if target_account is None or target_membership is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organisation")

            if TEAM_ROLE_HIERARCHY.get(req.role, -1) > ORG_ROLE_HIERARCHY.get(target_membership.role, -1):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Team role '{req.role}' exceeds user's org role '{target_membership.role}'",
                )

            membership = await add_team_member(
                session,
                org_id=current_user.organisation_id,
                team_id=team_id,
                account_id=user_id,
                role=req.role,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return MembershipResponse(
        id=str(membership.id),
        team_id=str(membership.team_id),
        user_id=str(membership.account_id),
        role=membership.role,
        created_at=membership.created_at.isoformat(),
    )


@router.delete(
    "/{team_id}/members/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member_endpoint(
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            membership = await get_membership(session, membership_id)
            if membership is None or membership.team_id != team_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")
            await remove_team_member(session, membership_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.patch(
    "/{team_id}/members/{membership_id}",
    response_model=MembershipResponse,
)
async def change_member_role_endpoint(
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
    req: ChangeMemberRoleRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            team = await get_team(session, team_id)
            if team is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

            is_admin = ORG_ROLE_HIERARCHY.get(current_user.org_role, -1) >= ORG_ROLE_HIERARCHY["admin"]
            if not is_admin:
                caller_membership = await get_membership_by_team_and_account(session, team_id, current_user.account_id)
                if caller_membership is None or caller_membership.role != "operator":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Only admin users or team operators can change member roles",
                    )
                if TEAM_ROLE_HIERARCHY.get(req.role, -1) > TEAM_ROLE_HIERARCHY.get(caller_membership.role, -1):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Cannot grant role '{req.role}' above your own team role '{caller_membership.role}'",
                    )

            membership = await update_member_role(session, membership_id, req.role)
            if membership is None or membership.team_id != team_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return MembershipResponse(
        id=str(membership.id),
        team_id=str(membership.team_id),
        user_id=str(membership.account_id),
        role=membership.role,
        created_at=membership.created_at.isoformat(),
    )
