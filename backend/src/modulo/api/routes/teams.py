"""Team management REST routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
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
    list_team_members,
    remove_team_member,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


class CreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


class UpdateTeamRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


class TeamResponse(BaseModel):
    id: str
    name: str
    description: str | None
    created_by: str
    created_at: str


class TeamListResponse(BaseModel):
    items: list[TeamResponse]
    total: int
    page: int
    page_size: int


class AddMemberRequest(BaseModel):
    user_id: str = Field(min_length=36, max_length=36)
    role: str = Field(default="member", pattern=r"^(member|admin)$")


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
    if principal.org_role != "admin":
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
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        result = await list_teams(session, org_id=current_user.organisation_id, page=page, page_size=page_size)

    return TeamListResponse(
        items=[
            TeamResponse(
                id=str(t.id),
                name=t.name,
                description=t.description,
                created_by=str(t.created_by),
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
    body: CreateTeamRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    _require_admin(current_user)

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        existing = await get_team_by_name(session, current_user.organisation_id, body.name)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A team with this name already exists in your organisation",
            )
        team = await create_team(
            session,
            org_id=current_user.organisation_id,
            name=body.name,
            created_by=current_user.user_id,
            description=body.description,
        )

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        created_by=str(team.created_by),
        created_at=team.created_at.isoformat(),
    )


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team_endpoint(
    team_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        team = await get_team(session, team_id)

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        created_by=str(team.created_by),
        created_at=team.created_at.isoformat(),
    )


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team_endpoint(
    team_id: uuid.UUID,
    body: UpdateTeamRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    _require_admin(current_user)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        team = await update_team(session, team_id, updates)

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        created_by=str(team.created_by),
        created_at=team.created_at.isoformat(),
    )


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team_endpoint(
    team_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user)

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        deleted = await delete_team(session, team_id)

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")


@router.get("/{team_id}/members", response_model=MembershipListResponse)
async def list_members_endpoint(
    team_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipListResponse:
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        result = await list_team_members(session, team_id=team_id, page=page, page_size=page_size)

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
    body: AddMemberRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipResponse:
    _require_admin(current_user)

    user_id = uuid.UUID(body.user_id)

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        team = await get_team(session, team_id)
        if team is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
        membership = await add_team_member(
            session,
            org_id=current_user.organisation_id,
            team_id=team_id,
            user_id=user_id,
            role=body.role,
        )

    return MembershipResponse(
        id=str(membership.id),
        team_id=str(membership.team_id),
        user_id=str(membership.user_id),
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

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.user_id, current_user.org_role)
        membership = await get_membership(session, membership_id)
        if membership is None or membership.team_id != team_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")
        await remove_team_member(session, membership_id)
