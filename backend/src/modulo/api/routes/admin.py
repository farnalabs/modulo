"""Admin-only routes for organisation, user, team, and billing management."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.db.crud.organisation import get_organisation, update_organisation
from modulo.db.crud.team import create_team, delete_team, list_teams
from modulo.db.crud.team import update_team as crud_update_team
from modulo.db.crud.user import create_user, get_user_by_email, list_users_paginated
from modulo.db.crud.user import update_user as crud_update_user
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.team import Team
from modulo.db.models.user import User
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ── Global Search ──────────────────────────────────────────────────────────


class SearchResultItem(BaseModel):
    type: str
    id: str
    title: str
    subtitle: str | None = None
    url: str


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    total_by_type: dict[str, int]


@router.get("/search", response_model=SearchResponse)
async def global_search(
    q: str = Query(min_length=1),
    type_filter: str = Query(
        default="all", alias="type", pattern=r"^(all|pipeline|run|audit|library)$"
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    if current_user.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        org_id = current_user.organisation_id
        like = f"%{q}%"
        prefix = f"{q}%"

        search_types: list[str] = (
            ["pipeline", "run", "audit", "library"]
            if type_filter == "all"
            else [type_filter]
        )

        all_items: list[tuple[int, SearchResultItem]] = []
        total_by_type: dict[str, int] = {"pipeline": 0, "run": 0, "audit": 0, "library": 0}

        for st in search_types:
            if st == "pipeline":
                rows = (
                    await session.execute(
                        text("""
                            SELECT id, name, description,
                                CASE WHEN name ILIKE :prefix THEN 2 ELSE 1 END AS relevance
                            FROM pipelines
                            WHERE organisation_id = :org_id
                                AND (name ILIKE :like OR description ILIKE :like)
                            ORDER BY relevance DESC, name ASC
                            LIMIT :lim OFFSET :off
                        """),
                        {
                            "org_id": org_id,
                            "like": like,
                            "prefix": prefix,
                            "lim": limit,
                            "off": offset,
                        },
                    )
                ).all()
                count = (
                    await session.execute(
                        text("""
                            SELECT COUNT(*) FROM pipelines
                            WHERE organisation_id = :org_id
                                AND (name ILIKE :like OR description ILIKE :like)
                        """),
                        {"org_id": org_id, "like": like},
                    )
                ).scalar() or 0

                for row in rows:
                    all_items.append((
                        row.relevance,
                        SearchResultItem(
                            type="pipeline",
                            id=str(row.id),
                            title=row.name,
                            subtitle=row.description,
                            url=f"/pipelines/{row.id}",
                        ),
                    ))
                total_by_type["pipeline"] = count

            elif st == "run":
                rows = (
                    await session.execute(
                        text("""
                            SELECT r.id, r.id::text AS display_id, p.name AS pipeline_name,
                                CASE WHEN r.id::text ILIKE :prefix THEN 2
                                     WHEN p.name ILIKE :like THEN 1 ELSE 0 END AS relevance
                            FROM runs r
                            JOIN pipelines p ON p.id = r.pipeline_id
                            WHERE r.organisation_id = :org_id
                                AND (r.id::text ILIKE :prefix OR p.name ILIKE :like)
                            ORDER BY relevance DESC, r.created_at DESC
                            LIMIT :lim OFFSET :off
                        """),
                        {
                            "org_id": org_id,
                            "like": like,
                            "prefix": prefix,
                            "lim": limit,
                            "off": offset,
                        },
                    )
                ).all()
                count = (
                    await session.execute(
                        text("""
                            SELECT COUNT(*) FROM runs r
                            JOIN pipelines p ON p.id = r.pipeline_id
                            WHERE r.organisation_id = :org_id
                                AND (r.id::text ILIKE :prefix OR p.name ILIKE :like)
                        """),
                        {"org_id": org_id, "like": like, "prefix": prefix},
                    )
                ).scalar() or 0

                for row in rows:
                    all_items.append((
                        row.relevance,
                        SearchResultItem(
                            type="run",
                            id=str(row.id),
                            title=row.display_id,
                            subtitle=row.pipeline_name,
                            url=f"/runs/{row.id}",
                        ),
                    ))
                total_by_type["run"] = count

            elif st == "audit":
                rows = (
                    await session.execute(
                        text("""
                            SELECT id, event_type, resource_type,
                                CASE WHEN event_type ILIKE :prefix THEN 2
                                     WHEN event_type ILIKE :like OR resource_type ILIKE :like
                                          OR payload_json::text ILIKE :like THEN 1
                                     ELSE 0 END AS relevance
                            FROM audit_events
                            WHERE organisation_id = :org_id
                                AND (event_type ILIKE :like OR resource_type ILIKE :like
                                     OR payload_json::text ILIKE :like)
                            ORDER BY relevance DESC, created_at DESC
                            LIMIT :lim OFFSET :off
                        """),
                        {
                            "org_id": org_id,
                            "like": like,
                            "prefix": prefix,
                            "lim": limit,
                            "off": offset,
                        },
                    )
                ).all()
                count = (
                    await session.execute(
                        text("""
                            SELECT COUNT(*) FROM audit_events
                            WHERE organisation_id = :org_id
                                AND (event_type ILIKE :like OR resource_type ILIKE :like
                                     OR payload_json::text ILIKE :like)
                        """),
                        {"org_id": org_id, "like": like},
                    )
                ).scalar() or 0

                for row in rows:
                    title = row.event_type
                    if row.resource_type:
                        title = f"{row.event_type} — {row.resource_type}"
                    all_items.append((
                        row.relevance,
                        SearchResultItem(
                            type="audit",
                            id=str(row.id),
                            title=title,
                            subtitle=None,
                            url=f"/admin/audit?event_id={row.id}",
                        ),
                    ))
                total_by_type["audit"] = count

            elif st == "library":
                rows = (
                    await session.execute(
                        text("""
                            SELECT id, name, description,
                                CASE WHEN name ILIKE :prefix THEN 2 ELSE 1 END AS relevance
                            FROM library_primitives
                            WHERE organisation_id = :org_id
                                AND (name ILIKE :like OR description ILIKE :like)
                            ORDER BY relevance DESC, name ASC
                            LIMIT :lim OFFSET :off
                        """),
                        {
                            "org_id": org_id,
                            "like": like,
                            "prefix": prefix,
                            "lim": limit,
                            "off": offset,
                        },
                    )
                ).all()
                count = (
                    await session.execute(
                        text("""
                            SELECT COUNT(*) FROM library_primitives
                            WHERE organisation_id = :org_id
                                AND (name ILIKE :like OR description ILIKE :like)
                        """),
                        {"org_id": org_id, "like": like},
                    )
                ).scalar() or 0

                for row in rows:
                    all_items.append((
                        row.relevance,
                        SearchResultItem(
                            type="library",
                            id=str(row.id),
                            title=row.name,
                            subtitle=row.description,
                            url="/libraries",
                        ),
                    ))
                total_by_type["library"] = count

        all_items.sort(key=lambda x: (-x[0], x[1].title))
        paginated = [item for _, item in all_items[offset : offset + limit]]

    return SearchResponse(results=paginated, total_by_type=total_by_type)


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    password: str = Field(min_length=8)
    org_role: str = Field(default="runner")

class CreateUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    org_role: str


@router.post("/users", response_model=CreateUserResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_user(
    body: CreateUserRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CreateUserResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can create users",
        )

    if body.org_role not in ("admin", "operator", "runner", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid role: {body.org_role}. "
                "Must be one of: admin, operator, runner, viewer"
            ),
        )

    existing = await get_user_by_email(session, body.email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    try:
        validate_password_strength(body.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    pw_hash = hash_password(body.password)
    user = await create_user(
        session,
        org_id=current_user.organisation_id,
        email=body.email,
        display_name=body.display_name,
        password_hash=pw_hash,
        org_role=body.org_role,
    )

    return CreateUserResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        org_role=user.org_role,
    )


class AdminCreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


class AdminUpdateTeamRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


class AdminCreateTeamResponse(BaseModel):
    id: str
    name: str
    description: str | None
    created_by: str
    created_at: str


@router.post("/teams", response_model=AdminCreateTeamResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_team(
    body: AdminCreateTeamRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminCreateTeamResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can create teams",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        team = await create_team(
            session,
            org_id=current_user.organisation_id,
            name=body.name,
            created_by=current_user.user_id,
            description=body.description,
        )

    return AdminCreateTeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        created_by=str(team.created_by),
        created_at=team.created_at.isoformat(),
    )


# ── Org Profile ───────────────────────────────────────────────


class UpdateOrgRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    logo_url: str | None = Field(None, max_length=2048)


class OrgProfileResponse(BaseModel):
    id: str
    name: str
    slug: str
    logo_url: str | None = None
    plan_id: str | None = None
    created_at: str


@router.put("/org", response_model=OrgProfileResponse)
async def admin_update_org(
    body: UpdateOrgRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgProfileResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update org profile",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        org = await get_organisation(session, current_user.organisation_id)
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organisation not found",
            )

        updates: dict[str, object] = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.logo_url is not None:
            existing_settings = dict(org.settings_json or {})
            existing_settings["logo_url"] = body.logo_url
            updates["settings_json"] = existing_settings

        if updates:
            updated = await update_organisation(
                session, current_user.organisation_id, updates
            )
            if updated is not None:
                org = updated

    current_settings = org.settings_json or {}
    return OrgProfileResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        logo_url=current_settings.get("logo_url"),
        plan_id=org.plan_id,
        created_at=org.created_at.isoformat(),
    )


@router.post("/org/regenerate-api-key", status_code=status.HTTP_200_OK)
async def admin_regenerate_api_key(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can regenerate API key",
        )

    from modulo.auth.api_key import create_api_key

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        _, raw_key = await create_api_key(
            session,
            org_id=current_user.organisation_id,
            created_by=current_user.user_id,
            name="Default Org API Key",
            role="operator",
        )

    return {"api_key": raw_key, "lookup_prefix": raw_key[3:11]}


# ── User Management ──────────────────────────────────────────


class UserListItem(BaseModel):
    id: str
    email: str
    display_name: str
    org_role: str
    is_active: bool
    auth_provider: str
    created_at: str
    last_login: str | None = None


class UserListResponse(BaseModel):
    items: list[UserListItem]
    total: int
    page: int
    page_size: int


@router.get("/users", response_model=UserListResponse)
async def admin_list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, min_length=1),
    role: str | None = Query(None, pattern=r"^(admin|operator|runner|viewer)$"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can list users",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        result = await list_users_paginated(
            session,
            org_id=current_user.organisation_id,
            page=page,
            page_size=page_size,
            search=search,
            role_filter=role,
        )

    return UserListResponse(
        items=[
            UserListItem(
                id=str(u.id),
                email=u.email,
                display_name=u.display_name,
                org_role=u.org_role,
                is_active=u.active,
                auth_provider=u.auth_provider,
                created_at=u.created_at.isoformat(),
                last_login=u.last_login.isoformat() if u.last_login else None,
            )
            for u in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


class UpdateUserRequest(BaseModel):
    org_role: str | None = Field(None, pattern=r"^(admin|operator|runner|viewer)$")
    is_active: bool | None = None


@router.put("/users/{user_id}", response_model=UserListItem)
async def admin_update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListItem:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update users",
        )

    updates: dict[str, object] = {}
    if body.org_role is not None:
        updates["org_role"] = body.org_role
    if body.is_active is not None:
        updates["active"] = body.is_active

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        user = await crud_update_user(session, user_id, updates)

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return UserListItem(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        org_role=user.org_role,
        is_active=user.active,
        auth_provider=user.auth_provider,
        created_at=user.created_at.isoformat(),
        last_login=user.last_login.isoformat() if user.last_login else None,
    )


@router.post("/users/{user_id}/deactivate", response_model=UserListItem)
async def admin_deactivate_user(
    user_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListItem:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can deactivate users",
        )

    if current_user.user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot deactivate yourself",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        user = await crud_update_user(session, user_id, {"active": False})

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return UserListItem(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        org_role=user.org_role,
        is_active=user.active,
        auth_provider=user.auth_provider,
        created_at=user.created_at.isoformat(),
        last_login=user.last_login.isoformat() if user.last_login else None,
    )


@router.post("/users/{user_id}/reactivate", response_model=UserListItem)
async def admin_reactivate_user(
    user_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListItem:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can reactivate users",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        user = await crud_update_user(session, user_id, {"active": True})

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return UserListItem(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        org_role=user.org_role,
        is_active=user.active,
        auth_provider=user.auth_provider,
        created_at=user.created_at.isoformat(),
        last_login=user.last_login.isoformat() if user.last_login else None,
    )


# ── Team Management ──────────────────────────────────────────


class AdminTeamItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    created_by: str
    member_count: int = 0
    created_at: str


class AdminTeamListResponse(BaseModel):
    items: list[AdminTeamItem]
    total: int
    page: int
    page_size: int


@router.get("/teams", response_model=AdminTeamListResponse)
async def admin_list_teams(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminTeamListResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can list teams",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        org_id = current_user.organisation_id
        result = await list_teams(session, org_id=org_id, page=page, page_size=page_size)

        # Enrich with member counts
        team_ids = [t.id for t in result.items]
        member_counts: dict[uuid.UUID, int] = {}
        if team_ids:
            count_rows = (
                await session.execute(
                    text("""
                        SELECT team_id, COUNT(*) AS cnt
                        FROM team_memberships
                        WHERE team_id = ANY(:team_ids)
                        GROUP BY team_id
                    """),
                    {"team_ids": team_ids},
                )
            ).all()
            for row in count_rows:
                member_counts[row.team_id] = row.cnt

    return AdminTeamListResponse(
        items=[
            AdminTeamItem(
                id=str(t.id),
                name=t.name,
                description=t.description,
                created_by=str(t.created_by),
                member_count=member_counts.get(t.id, 0),
                created_at=t.created_at.isoformat(),
            )
            for t in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.put("/teams/{team_id}", response_model=AdminTeamItem)
async def admin_update_team(
    team_id: uuid.UUID,
    body: AdminUpdateTeamRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminTeamItem:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update teams",
        )

    updates: dict[str, object] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        team = await crud_update_team(session, team_id, updates)

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return AdminTeamItem(
        id=str(team.id),
        name=team.name,
        description=team.description,
        created_by=str(team.created_by),
        created_at=team.created_at.isoformat(),
    )


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_team(
    team_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can delete teams",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        # Check for pipelines referencing this team
        pipeline_count = (
            await session.execute(
                select(func.count()).select_from(Pipeline).where(Pipeline.owner_team_id == team_id)
            )
        ).scalar() or 0
        if pipeline_count > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cannot delete team: {pipeline_count} pipeline(s) "
                    "still reference this team"
                ),
            )

        deleted = await delete_team(session, team_id)

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")


# ── Billing Overview ─────────────────────────────────────────


class BillingOverviewResponse(BaseModel):
    plan_id: str | None = None
    plan_tier: str = "free"
    daily_spend_limit: float | None = None
    total_users: int = 0
    total_teams: int = 0
    total_pipelines: int = 0
    total_runs_this_month: int = 0
    license_key: str | None = None


@router.get("/billing/overview", response_model=BillingOverviewResponse)
async def admin_billing_overview(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> BillingOverviewResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can view billing",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        org = await get_organisation(session, current_user.organisation_id)
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organisation not found",
            )

        org_id = current_user.organisation_id
        user_count = (
            await session.execute(
                select(func.count()).select_from(User).where(User.organisation_id == org_id)
            )
        ).scalar() or 0

        team_count = (
            await session.execute(
                select(func.count()).select_from(Team).where(Team.organisation_id == org_id)
            )
        ).scalar() or 0

        pipeline_count = (
            await session.execute(
                select(func.count()).select_from(Pipeline).where(Pipeline.organisation_id == org_id)
            )
        ).scalar() or 0

        now = datetime.now(UTC)
        runs_this_month = (
            await session.execute(
                text("""
                    SELECT COUNT(*) FROM runs
                    WHERE organisation_id = :org_id
                        AND created_at >= date_trunc('month', :now::timestamp)
                """),
                {"org_id": current_user.organisation_id, "now": now},
            )
        ).scalar() or 0

    plan_id = org.plan_id or "free"
    if plan_id and plan_id.startswith("enterprise"):
        plan_tier = "enterprise"
    elif plan_id and plan_id != "free":
        plan_tier = "pro"
    else:
        plan_tier = "free"

    settings = org.settings_json or {}
    return BillingOverviewResponse(
        plan_id=plan_id,
        plan_tier=plan_tier,
        daily_spend_limit=float(org.daily_spend_limit) if org.daily_spend_limit else None,
        total_users=user_count,
        total_teams=team_count,
        total_pipelines=pipeline_count,
        total_runs_this_month=runs_this_month,
        license_key=settings.get("license_key"),
    )


# ── Org Deletion ─────────────────────────────────────────────────────


def _require_org_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can manage organisation deletion",
        )


class DeletionRequestResponse(BaseModel):
    message: str
    token: str
    token_expires_at: str
    export_summary: dict[str, object]


@router.post(
    "/org/deletion-request",
    response_model=DeletionRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_org_deletion(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> DeletionRequestResponse:
    _require_org_admin(current_user)

    from modulo.core.audit_logger import append_audit_event
    from modulo.db.crud.org_deletion import request_org_deletion as _request_deletion

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        try:
            result = await _request_deletion(
                session,
                org_id=current_user.organisation_id,
                actor_user_id=current_user.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        await append_audit_event(
            session,
            org_id=current_user.organisation_id,
            event_type="org_deletion_requested",
            actor_user_id=current_user.user_id,
            resource_type="organisation",
            resource_id=current_user.organisation_id,
            payload_json={
                "deletion_token": result["token"][:12] + "...",
                "token_expires_at": result["token_expires_at"],
                "exported_entities": list(result["export"].keys()),
            },
        )

    export = result["export"]
    return DeletionRequestResponse(
        message="Deletion requested. A confirmation link has been generated (valid for 24 h).",
        token=result["token"],
        token_expires_at=result["token_expires_at"],
        export_summary={
            "organisation": export.get("organisation", [{}])[0].get("name", "unknown"),
            "user_count": len(export.get("users", [])),
            "pipeline_count": len(export.get("pipelines", [])),
            "run_count": len(export.get("runs", [])),
            "audit_event_count": len(export.get("audit_events", [])),
            "library_count": len(export.get("library_primitives", [])),
            "connector_count": len(export.get("connector_instances", [])),
            "backend_count": len(export.get("model_backends", [])),
        },
    )


class ConfirmDeletionRequest(BaseModel):
    token: str


class ConfirmDeletionResponse(BaseModel):
    message: str
    deleted_organisation_id: str
    hard_deleted_runs: int


@router.post("/org/deletion-confirm", response_model=ConfirmDeletionResponse)
async def confirm_org_deletion(
    body: ConfirmDeletionRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmDeletionResponse:
    _require_org_admin(current_user)

    from modulo.db.crud.org_deletion import confirm_org_deletion as _confirm_deletion

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        try:
            result = await _confirm_deletion(
                session,
                org_id=current_user.organisation_id,
                token=body.token,
                immediate=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ConfirmDeletionResponse(
        message="Organisation has been permanently deleted.",
        deleted_organisation_id=result["deleted_organisation_id"],
        hard_deleted_runs=result["hard_deleted_runs"],
    )


class OrgExportResponse(BaseModel):
    organisation: dict[str, object]
    exported_at: str


@router.get("/org/export", response_model=OrgExportResponse)
async def export_org_data(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgExportResponse:
    _require_org_admin(current_user)

    from modulo.db.crud.org_deletion import export_org_data as _export

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        try:
            bundle = await _export(session, org_id=current_user.organisation_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    org_info = (bundle.get("organisation") or [{}])[0]
    return OrgExportResponse(
        organisation={
            "id": str(org_info.get("id", "")),
            "name": org_info.get("name", ""),
            "slug": org_info.get("slug", ""),
            "status": org_info.get("status", ""),
            "created_at": str(org_info.get("created_at", "")),
        },
        exported_at=bundle.get("exported_at", ""),
    )


@router.delete("/org", response_model=ConfirmDeletionResponse)
async def delete_org_immediate(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmDeletionResponse:
    _require_org_admin(current_user)

    from modulo.core.audit_logger import append_audit_event
    from modulo.db.crud.org_deletion import confirm_org_deletion as _confirm_deletion
    from modulo.db.crud.org_deletion import request_org_deletion as _request_deletion

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        try:
            req = await _request_deletion(
                session,
                org_id=current_user.organisation_id,
                actor_user_id=current_user.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        await append_audit_event(
            session,
            org_id=current_user.organisation_id,
            event_type="org_deletion_requested",
            actor_user_id=current_user.user_id,
            resource_type="organisation",
            resource_id=current_user.organisation_id,
            payload_json={"immediate": True, "exported_entities": list(req["export"].keys())},
        )

        result = await _confirm_deletion(
            session,
            org_id=current_user.organisation_id,
            token=req["token"],
            immediate=True,
        )

    return ConfirmDeletionResponse(
        message="Organisation has been permanently deleted.",
        deleted_organisation_id=result["deleted_organisation_id"],
        hard_deleted_runs=result["hard_deleted_runs"],
    )


# ── Eval Dashboard ──────────────────────────────────────────────────────


class EvalDashboardSummary(BaseModel):
    total_results: int
    passed: int
    failed: int
    pass_rate: float
    total_definitions: int


class TrendBucket(BaseModel):
    bucket: str
    total: int
    passed: int
    failed: int


class TypeBreakdown(BaseModel):
    eval_type: str
    total: int
    passed: int
    failed: int


class CoverageGap(BaseModel):
    pipeline_id: str
    pipeline_name: str
    node_id: str


class RecentEvalResult(BaseModel):
    id: str
    eval_id: str
    eval_name: str
    eval_type: str
    passed: bool
    score: float | None
    detail: str | None
    evaluated_at: str


class EvalDashboardResponse(BaseModel):
    summary: EvalDashboardSummary
    trend: list[TrendBucket]
    by_type: list[TypeBreakdown]
    coverage_gaps: list[CoverageGap]
    recent_results: list[RecentEvalResult]


@router.get("/evals/dashboard", response_model=EvalDashboardResponse)
async def eval_dashboard(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> EvalDashboardResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can access the eval dashboard",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        # ── Summary ─────────────────────────────────────────────────
        summary_q = select(
            func.count(EvalResult.id).label("total_results"),
            func.sum(
                case((EvalResult.passed, 1), else_=0)
            ).label("passed"),
            func.sum(
                case((EvalResult.passed.is_(False), 1), else_=0)
            ).label("failed"),
        )
        summary_row = (await session.execute(summary_q)).one()

        defs_q = select(func.count(EvalDefinition.id)).select_from(EvalDefinition)
        total_defs = (await session.execute(defs_q)).scalar() or 0

        total_results = summary_row.total_results or 0
        passed = summary_row.passed or 0
        failed = summary_row.failed or 0
        pass_rate = round(passed / total_results, 4) if total_results > 0 else 0.0

        summary = EvalDashboardSummary(
            total_results=total_results,
            passed=passed,
            failed=failed,
            pass_rate=pass_rate,
            total_definitions=total_defs,
        )

        # ── Trend (daily buckets) ───────────────────────────────────
        trend_q = text("""
            SELECT
                date_trunc('day', evaluated_at) AS bucket,
                COUNT(*) AS total,
                SUM(CASE WHEN passed THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN NOT passed THEN 1 ELSE 0 END) AS failed
            FROM eval_results
            WHERE organisation_id = :org_id
            GROUP BY bucket
            ORDER BY bucket
        """)
        trend_rows = (
            await session.execute(trend_q, {"org_id": current_user.organisation_id})
        ).all()

        trend = [
            TrendBucket(
                bucket=str(row.bucket),
                total=row.total,
                passed=row.passed,
                failed=row.failed,
            )
            for row in trend_rows
        ]

        # ── By eval type ────────────────────────────────────────────
        by_type_q = text("""
            SELECT
                ed.eval_type,
                COUNT(er.id) AS total,
                SUM(CASE WHEN er.passed THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN NOT er.passed THEN 1 ELSE 0 END) AS failed
            FROM eval_definitions ed
            LEFT JOIN eval_results er ON er.eval_id = ed.id
            WHERE ed.organisation_id = :org_id
            GROUP BY ed.eval_type
            ORDER BY ed.eval_type
        """)
        by_type_rows = (
            await session.execute(by_type_q, {"org_id": current_user.organisation_id})
        ).all()

        by_type = [
            TypeBreakdown(
                eval_type=row.eval_type,
                total=row.total,
                passed=row.passed,
                failed=row.failed,
            )
            for row in by_type_rows
        ]

        # ── Coverage gaps ───────────────────────────────────────────
        pipelines = (
            await session.execute(
                select(Pipeline.id, Pipeline.name, Pipeline.graph_nodes_json).where(
                    Pipeline.organisation_id == current_user.organisation_id
                )
            )
        ).all()

        covered_pairs: set[tuple[uuid.UUID, str]] = set()
        eval_defs = (
            await session.execute(
                select(EvalDefinition.pipeline_id, EvalDefinition.node_id).where(
                    EvalDefinition.organisation_id == current_user.organisation_id
                )
            )
        ).all()
        for ed in eval_defs:
            if ed.node_id is not None:
                covered_pairs.add((ed.pipeline_id, str(ed.node_id)))

        coverage_gaps: list[CoverageGap] = []
        for pl in pipelines:
            for node in (pl.graph_nodes_json or []):
                node_id = node.get("id")
                if node_id and (pl.id, str(node_id)) not in covered_pairs:
                    coverage_gaps.append(
                        CoverageGap(
                            pipeline_id=str(pl.id),
                            pipeline_name=pl.name,
                            node_id=str(node_id),
                        )
                    )

        # ── Recent results ──────────────────────────────────────────
        recent_q = text("""
            SELECT
                er.id,
                er.eval_id,
                ed.name AS eval_name,
                ed.eval_type,
                er.passed,
                er.score,
                er.detail,
                er.evaluated_at
            FROM eval_results er
            JOIN eval_definitions ed ON ed.id = er.eval_id
            WHERE er.organisation_id = :org_id
            ORDER BY er.evaluated_at DESC
            LIMIT 50
        """)
        recent_rows = (
            await session.execute(recent_q, {"org_id": current_user.organisation_id})
        ).all()

        recent_results = [
            RecentEvalResult(
                id=str(row.id),
                eval_id=str(row.eval_id),
                eval_name=row.eval_name,
                eval_type=row.eval_type,
                passed=row.passed,
                score=row.score,
                detail=row.detail,
                evaluated_at=str(row.evaluated_at),
            )
            for row in recent_rows
        ]

    return EvalDashboardResponse(
        summary=summary,
        trend=trend,
        by_type=by_type,
        coverage_gaps=coverage_gaps,
        recent_results=recent_results,
    )
