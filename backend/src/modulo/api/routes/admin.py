"""Admin-only routes for organisation, user, team, and billing management."""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_feature
from modulo.auth.api_key import revoke_api_key
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.core.eval_engine.okr import track_okr_progress
from modulo.core.eval_engine.regression import detect_regressions
from modulo.core.hitl_manager.overdue_warning import get_overdue_claims
from modulo.db.crud.account import get_account_by_email, get_account_by_id
from modulo.db.crud.org_membership import create_membership
from modulo.db.crud.organisation import get_organisation, update_organisation
from modulo.db.crud.publisher import (
    create_publisher,
    get_publisher_by_key,
    get_publisher_by_name,
    list_publishers,
)
from modulo.db.crud.publisher import (
    delete_publisher as crud_delete_publisher,
)
from modulo.db.crud.publisher import (
    update_publisher as crud_update_publisher,
)
from modulo.db.crud.run import batch_delete_old_terminal_runs, purge_runs
from modulo.db.crud.team import create_team, delete_team, list_teams
from modulo.db.crud.team import update_team as crud_update_team
from modulo.db.crud.team_membership import list_team_memberships_for_account, remove_team_member
from modulo.db.crud.token_family import blacklist_family, list_families_for_account
from modulo.db.models.account import Account
from modulo.db.models.api_key import OrgApiKey
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.stage import Stage
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org, set_rls_user_context

logger = logging.getLogger(__name__)

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
    type_filter: str = Query(default="all", alias="type", pattern=r"^(all|pipeline|run|audit|library)$"),
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

        search_types: list[str] = ["pipeline", "run", "audit", "library"] if type_filter == "all" else [type_filter]

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
                    all_items.append(
                        (
                            row.relevance,
                            SearchResultItem(
                                type="pipeline",
                                id=str(row.id),
                                title=row.name,
                                subtitle=row.description,
                                url=f"/pipelines/{row.id}",
                            ),
                        )
                    )
                total_by_type["pipeline"] = count

            elif st == "run":
                rows = (
                    await session.execute(
                        text("""
                            SELECT r.id, r.run_number, r.id::text AS display_id, p.name AS pipeline_name,
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
                    display_id = f"#{row.run_number}" if row.run_number is not None else f"#{str(row.id)[:8]}"
                    all_items.append(
                        (
                            row.relevance,
                            SearchResultItem(
                                type="run",
                                id=str(row.id),
                                title=display_id,
                                subtitle=row.pipeline_name,
                                url=f"/runs/{row.id}",
                            ),
                        )
                    )
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
                    all_items.append(
                        (
                            row.relevance,
                            SearchResultItem(
                                type="audit",
                                id=str(row.id),
                                title=title,
                                subtitle=None,
                                url=f"/admin/audit?event_id={row.id}",
                            ),
                        )
                    )
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
                    all_items.append(
                        (
                            row.relevance,
                            SearchResultItem(
                                type="library",
                                id=str(row.id),
                                title=row.name,
                                subtitle=row.description,
                                url="/libraries",
                            ),
                        )
                    )
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
    req: CreateUserRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CreateUserResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can create users",
        )

    if req.org_role not in ("admin", "operator", "runner", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"Invalid role: {req.org_role}. Must be one of: admin, operator, runner, viewer"),
        )

    existing = await get_account_by_email(session, req.email)
    if existing is not None:
        from modulo.db.crud.org_membership import get_membership_by_account_and_org

        membership = await get_membership_by_account_and_org(session, existing.id, current_user.organisation_id)
        if membership is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email already exists in this organisation",
            )

    try:
        validate_password_strength(req.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    pw_hash = hash_password(req.password)

    if existing is not None:
        account = existing
        account.password_hash = pw_hash
    else:
        from modulo.db.crud.account import create_account

        account = await create_account(
            session,
            email=req.email,
            display_name=req.display_name,
            password_hash=pw_hash,
        )

    membership = await create_membership(
        session,
        account_id=account.id,
        org_id=current_user.organisation_id,
        role=req.org_role,
    )

    return CreateUserResponse(
        id=str(account.id),
        email=account.email,
        display_name=account.display_name,
        org_role=membership.role,
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
    account_id: str
    created_at: str


@router.post(
    "/teams",
    response_model=AdminCreateTeamResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_feature("team_rbac")],
)
async def admin_create_team(
    req: AdminCreateTeamRequest,
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
            name=req.name,
            account_id=current_user.account_id,
            description=req.description,
        )

    return AdminCreateTeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
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


@router.get("/org", response_model=OrgProfileResponse)
async def admin_get_org(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgProfileResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can view org profile",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Organisation not found",
                )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    current_settings = org.settings_json or {}
    return OrgProfileResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        logo_url=current_settings.get("logo_url"),
        plan_id=org.plan_id,
        created_at=org.created_at.isoformat(),
    )


@router.put("/org", response_model=OrgProfileResponse)
async def admin_update_org(
    req: UpdateOrgRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgProfileResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update org profile",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Organisation not found",
                )

            updates: dict[str, object] = {}
            if req.name is not None:
                updates["name"] = req.name
            if req.logo_url is not None:
                existing_settings = dict(org.settings_json or {})
                existing_settings["logo_url"] = req.logo_url
                updates["settings_json"] = existing_settings

            if updates:
                updated = await update_organisation(session, current_user.organisation_id, updates)
                if updated is not None:
                    org = updated
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

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

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            _, raw_key = await create_api_key(
                session,
                org_id=current_user.organisation_id,
                account_id=current_user.account_id,
                name="Default Org API Key",
                role="operator",
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
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
        accounts_memberships, total = await _list_org_accounts(
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
                id=str(a.id),
                email=a.email,
                display_name=a.display_name,
                org_role=m.role,
                is_active=a.active,
                auth_provider=a.auth_provider,
                created_at=a.created_at.isoformat(),
                last_login=a.last_login.isoformat() if a.last_login else None,
            )
            for a, m in accounts_memberships
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


async def _list_org_accounts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    role_filter: str | None = None,
) -> tuple[list[tuple[Account, object]], int]:
    conditions = [OrgMembership.organisation_id == org_id]
    if search:
        conditions.append(Account.email.ilike(f"%{search}%"))
    if role_filter:
        conditions.append(OrgMembership.role == role_filter)

    count_q = (
        select(func.count())
        .select_from(OrgMembership)
        .join(Account, Account.id == OrgMembership.account_id)
        .where(*conditions)
    )
    total = (await session.execute(count_q)).scalar() or 0

    query = (
        select(Account, OrgMembership)
        .join(OrgMembership, Account.id == OrgMembership.account_id)
        .where(*conditions)
        .order_by(Account.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(query)
    return list(result.all()), total


class UpdateUserRequest(BaseModel):
    org_role: str | None = Field(None, pattern=r"^(admin|operator|runner|viewer)$")
    is_active: bool | None = None


async def _prevent_last_admin_lockout(
    current_account_id: uuid.UUID,
    target_account_id: uuid.UUID,
    org_id: uuid.UUID,
    new_role: str | None,
    db_session: AsyncSession,
) -> None:
    if target_account_id != current_account_id:
        return
    if new_role is None or new_role == "admin":
        return

    result = await db_session.execute(
        select(func.count()).where(
            OrgMembership.organisation_id == org_id,
            OrgMembership.role == "admin",
        )
    )
    admin_count = result.scalar() or 0

    if admin_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot remove the last admin. Promote another user to admin first.",
        )


@router.put("/users/{user_id}", response_model=UserListItem)
async def admin_update_user(
    user_id: uuid.UUID,
    req: UpdateUserRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListItem:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update users",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await _prevent_last_admin_lockout(
            current_account_id=current_user.account_id,
            target_account_id=user_id,
            org_id=current_user.organisation_id,
            new_role=req.org_role,
            db_session=session,
        )

        account = await get_account_by_id(session, user_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        if req.is_active is not None:
            account.active = req.is_active
        if req.org_role is not None:
            from sqlalchemy import update as sa_update

            await session.execute(
                sa_update(OrgMembership)
                .where(
                    OrgMembership.account_id == user_id,
                    OrgMembership.organisation_id == current_user.organisation_id,
                )
                .values(role=req.org_role)
            )

    org_role = req.org_role or (await _get_org_role(session, user_id, current_user.organisation_id))
    return UserListItem(
        id=str(account.id),
        email=account.email,
        display_name=account.display_name,
        org_role=org_role,
        is_active=account.active,
        auth_provider=account.auth_provider,
        created_at=account.created_at.isoformat(),
        last_login=account.last_login.isoformat() if account.last_login else None,
    )


async def _get_org_role(session: AsyncSession, account_id: uuid.UUID, org_id: uuid.UUID) -> str:
    from modulo.db.crud.org_membership import get_membership_by_account_and_org

    membership = await get_membership_by_account_and_org(session, account_id, org_id)
    return membership.role if membership is not None else ""


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

    if current_user.account_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot deactivate yourself",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            account = await get_account_by_id(session, user_id)
            if account is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found",
                )
            account.active = False

            families = await list_families_for_account(session, user_id)
            for family in families:
                await blacklist_family(session, family.family_id)

            active_keys = (
                await session.execute(
                    select(OrgApiKey).where(
                        OrgApiKey.account_id == user_id,
                        OrgApiKey.revoked_at.is_(None),
                    )
                )
            ).scalars().all()
            for key in active_keys:
                await revoke_api_key(session, key.id, current_user.organisation_id)

            team_memberships = await list_team_memberships_for_account(session, user_id)
            for tm in team_memberships:
                await remove_team_member(session, tm.id)

            from modulo.core.audit_logger import append_audit_event

            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="user_deactivated",
                actor_user_id=current_user.account_id,
                resource_type="user",
                resource_id=user_id,
                payload_json={"target_user_id": str(user_id)},
            )

            await session.flush()
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    org_role = await _get_org_role(session, user_id, current_user.organisation_id)
    return UserListItem(
        id=str(account.id),
        email=account.email,
        display_name=account.display_name,
        org_role=org_role,
        is_active=account.active,
        auth_provider=account.auth_provider,
        created_at=account.created_at.isoformat(),
        last_login=account.last_login.isoformat() if account.last_login else None,
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

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            account = await get_account_by_id(session, user_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            account.active = True

            from modulo.core.audit_logger import append_audit_event

            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="user_reactivated",
                actor_user_id=current_user.account_id,
                resource_type="user",
                resource_id=user_id,
                payload_json={"target_user_id": str(user_id)},
            )

            await session.flush()
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    org_role = await _get_org_role(session, user_id, current_user.organisation_id)
    return UserListItem(
        id=str(account.id),
        email=account.email,
        display_name=account.display_name,
        org_role=org_role,
        is_active=account.active,
        auth_provider=account.auth_provider,
        created_at=account.created_at.isoformat(),
        last_login=account.last_login.isoformat() if account.last_login else None,
    )


class AdminResetPasswordResponse(BaseModel):
    temporary_password: str


@router.post("/users/{user_id}/reset-password", response_model=AdminResetPasswordResponse)
async def admin_reset_password(
    user_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminResetPasswordResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can reset passwords",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        account = await get_account_by_id(session, user_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        temporary_password = secrets.token_urlsafe(18)[:24]
        account.password_hash = hash_password(temporary_password)

        families = await list_families_for_account(session, user_id)
        for family in families:
            await blacklist_family(session, family.family_id)

        await session.flush()

    return AdminResetPasswordResponse(temporary_password=temporary_password)


# ── Team Management ──────────────────────────────────────────


class AdminTeamItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    account_id: str
    member_count: int = 0
    created_at: str


class AdminTeamListResponse(BaseModel):
    items: list[AdminTeamItem]
    total: int
    page: int
    page_size: int


@router.get("/teams", response_model=AdminTeamListResponse, dependencies=[require_feature("team_rbac")])
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
                account_id=str(t.account_id),
                member_count=member_counts.get(t.id, 0),
                created_at=t.created_at.isoformat(),
            )
            for t in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.put("/teams/{team_id}", response_model=AdminTeamItem, dependencies=[require_feature("team_rbac")])
async def admin_update_team(
    team_id: uuid.UUID,
    req: AdminUpdateTeamRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminTeamItem:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update teams",
        )

    updates: dict[str, object] = {}
    if req.name is not None:
        updates["name"] = req.name
    if req.description is not None:
        updates["description"] = req.description

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        team = await crud_update_team(session, team_id, updates)

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return AdminTeamItem(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat(),
    )


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[require_feature("team_rbac")])
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

    try:
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
        logger.warning("Failed to record team_deleted audit event for team %s", team_id)


# ── Billing Overview ─────────────────────────────────────────


class BillingOverviewResponse(BaseModel):
    plan_id: str | None = None
    plan_tier: str = "community"
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

    try:
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
                    select(func.count()).select_from(OrgMembership).where(OrgMembership.organisation_id == org_id)
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

            month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            runs_this_month = (
                await session.execute(
                    select(func.count(Run.id)).where(
                        Run.organisation_id == current_user.organisation_id,
                        Run.created_at >= month_start,
                    )
                )
            ).scalar() or 0
    except Exception:
        logger.exception("billing.overview_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing overview is temporarily unavailable.",
        ) from None

    plan_id = org.plan_id or "community"
    if plan_id and plan_id.startswith("team"):
        plan_tier = "team"
    elif plan_id and plan_id != "community":
        plan_tier = "pro"
    else:
        plan_tier = "community"

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
    if principal.is_system_admin:
        return
    if principal.org_role not in ("admin", "owner"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or owner users can manage organisation deletion",
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

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            try:
                result = await _request_deletion(
                    session,
                    org_id=current_user.organisation_id,
                    actor_user_id=current_user.account_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="org_deletion_requested",
                actor_user_id=current_user.account_id,
                resource_type="organisation",
                resource_id=current_user.organisation_id,
                payload_json={
                    "deletion_token": result["token"][:12] + "...",
                    "token_expires_at": result["token_expires_at"],
                    "exported_entities": list(result["export"].keys()),
                },
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
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
    req: ConfirmDeletionRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmDeletionResponse:
    _require_org_admin(current_user)

    from modulo.db.crud.org_deletion import confirm_org_deletion as _confirm_deletion

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            try:
                result = await _confirm_deletion(
                    session,
                    org_id=current_user.organisation_id,
                    token=req.token,
                    immediate=False,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    return ConfirmDeletionResponse(
        message="Organisation has been permanently deleted.",
        deleted_organisation_id=result["deleted_organisation_id"],
        hard_deleted_runs=result["hard_deleted_runs"],
    )


class CancelDeletionResponse(BaseModel):
    status: str


class OrgExportResponse(BaseModel):
    organisation: dict[str, object]
    exported_at: str


@router.patch("/org/deletion-cancel", response_model=CancelDeletionResponse)
async def cancel_org_deletion(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CancelDeletionResponse:
    _require_org_admin(current_user)

    from modulo.db.crud.org_deletion import cancel_org_deletion as _cancel

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            try:
                result = await _cancel(session, org_id=current_user.organisation_id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    return CancelDeletionResponse(**result)


@router.get("/org/export", response_model=OrgExportResponse)
async def export_org_data(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgExportResponse:
    _require_org_admin(current_user)

    from modulo.db.crud.org_deletion import export_org_data as _export

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            try:
                bundle = await _export(session, org_id=current_user.organisation_id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

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

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            try:
                req = await _request_deletion(
                    session,
                    org_id=current_user.organisation_id,
                    actor_user_id=current_user.account_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="org_deletion_requested",
                actor_user_id=current_user.account_id,
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
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
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

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            # ── Summary ─────────────────────────────────────────────────
            summary_q = select(
                func.count(EvalResult.id).label("total_results"),
                func.sum(case((EvalResult.passed, 1), else_=0)).label("passed"),
                func.sum(case((EvalResult.passed.is_(False), 1), else_=0)).label("failed"),
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
            trend_rows = (await session.execute(trend_q, {"org_id": current_user.organisation_id})).all()

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
            by_type_rows = (await session.execute(by_type_q, {"org_id": current_user.organisation_id})).all()

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
                for node in pl.graph_nodes_json or []:
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
            recent_rows = (await session.execute(recent_q, {"org_id": current_user.organisation_id})).all()

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
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    return EvalDashboardResponse(
        summary=summary,
        trend=trend,
        by_type=by_type,
        coverage_gaps=coverage_gaps,
        recent_results=recent_results,
    )


# ── Eval Regression Alerts ────────────────────────────────────────────────


class RegressionAlertResponse(BaseModel):
    eval_id: str
    eval_name: str
    prev_pass_rate: float
    current_pass_rate: float
    drop_pct: float
    trend: str
    affected_run_ids: list[str]


class RegressionAlertsResponse(BaseModel):
    alerts: list[RegressionAlertResponse]
    total_regressions: int
    threshold: float
    lookback_days: int


@router.get("/evals/regressions", response_model=RegressionAlertsResponse)
async def eval_regressions(
    days: int = Query(default=7, ge=1, le=90, description="Lookback period in days"),
    threshold: float = Query(default=0.15, ge=0.0, le=1.0, description="Minimum drop fraction to trigger an alert"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> RegressionAlertsResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can access eval regressions",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            alerts = await detect_regressions(
                session,
                org_id=current_user.organisation_id,
                days=days,
                threshold=threshold,
            )
    except ProgrammingError:
        logger.warning("Eval regressions unavailable — DB may need migration")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        logger.error("Eval regressions DB error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again later.",
        )

    return RegressionAlertsResponse(
        alerts=[
            RegressionAlertResponse(
                eval_id=str(a.eval_id),
                eval_name=a.eval_name,
                prev_pass_rate=a.prev_pass_rate,
                current_pass_rate=a.current_pass_rate,
                drop_pct=a.drop_pct,
                trend=a.trend,
                affected_run_ids=[str(rid) for rid in a.affected_run_ids],
            )
            for a in alerts
        ],
        total_regressions=len(alerts),
        threshold=threshold,
        lookback_days=days,
    )


# ── OKR-Aligned Eval Suite Progress ────────────────────────────────────────


class OkrTrendPointResponse(BaseModel):
    period: str
    pass_rate: float
    total_evals: int
    passed_evals: int


class OkrProgressResponse(BaseModel):
    suite_id: str
    suite_name: str
    current_score: float
    pass_threshold: float | None
    trend: list[OkrTrendPointResponse]
    trend_direction: str
    days_to_target: int | None
    breach: bool


@router.get("/evals/okr-progress/{suite_id}", response_model=OkrProgressResponse)
async def okr_progress(
    suite_id: str,
    target_date: str | None = Query(
        default=None,
        description="Optional ISO 8601 target date (e.g. 2026-09-30) for days-to-target",
    ),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OkrProgressResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can access OKR progress",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            progress = await track_okr_progress(
                session,
                org_id=current_user.organisation_id,
                suite_id=suite_id,
                target_date=target_date,
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    return OkrProgressResponse(
        suite_id=progress.suite_id,
        suite_name=progress.suite_name,
        current_score=progress.current_score,
        pass_threshold=progress.pass_threshold,
        trend=[
            OkrTrendPointResponse(
                period=t.period,
                pass_rate=t.pass_rate,
                total_evals=t.total_evals,
                passed_evals=t.passed_evals,
            )
            for t in progress.trend
        ],
        trend_direction=progress.trend_direction,
        days_to_target=progress.days_to_target,
        breach=progress.breach,
    )


# ── Publisher Management ──────────────────────────────────────────────────


class PublisherCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact_email: str | None = Field(None, max_length=255)
    public_key_hex: str = Field(min_length=64, max_length=128)
    trust_tier: str = Field(default="amber", pattern=r"^(green|amber)$")
    website_url: str | None = Field(None, max_length=2000)


class PublisherUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    contact_email: str | None = Field(None, max_length=255)
    public_key_hex: str | None = Field(None, min_length=64, max_length=128)
    trust_tier: str | None = Field(None, pattern=r"^(green|amber)$")
    website_url: str | None = Field(None, max_length=2000)


class PublisherResponse(BaseModel):
    id: str
    name: str
    contact_email: str | None
    public_key_hex: str
    trust_tier: str
    verified_since: str | None
    website_url: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class PublisherListResponse(BaseModel):
    items: list[PublisherResponse]
    total: int
    page: int
    page_size: int


@router.get("/publishers", response_model=PublisherListResponse)
async def admin_list_publishers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    trust_tier: str | None = Query(None, pattern=r"^(green|amber)$"),
    search: str | None = Query(None, min_length=1),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> PublisherListResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can list publishers",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            result = await list_publishers(
                session,
                org_id=current_user.organisation_id,
                page=page,
                page_size=page_size,
                trust_tier=trust_tier,
                search=search,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    return PublisherListResponse(
        items=[
            PublisherResponse(
                id=str(p.id),
                name=p.name,
                contact_email=p.contact_email,
                public_key_hex=p.public_key_hex,
                trust_tier=p.trust_tier,
                verified_since=p.verified_since.isoformat() if p.verified_since else None,
                website_url=p.website_url,
                created_at=p.created_at.isoformat(),
                updated_at=p.updated_at.isoformat(),
            )
            for p in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("/publishers", response_model=PublisherResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_publisher(
    req: PublisherCreateRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> PublisherResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can create publishers",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            existing = await get_publisher_by_name(session, current_user.organisation_id, req.name)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A publisher with this name already exists",
                )

            existing_key = await get_publisher_by_key(session, current_user.organisation_id, req.public_key_hex)
            if existing_key is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A publisher with this public key already exists",
                )

            try:
                publisher = await create_publisher(
                    session,
                    org_id=current_user.organisation_id,
                    name=req.name,
                    contact_email=req.contact_email,
                    public_key_hex=req.public_key_hex,
                    trust_tier=req.trust_tier,
                    website_url=req.website_url,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    return PublisherResponse(
        id=str(publisher.id),
        name=publisher.name,
        contact_email=publisher.contact_email,
        public_key_hex=publisher.public_key_hex,
        trust_tier=publisher.trust_tier,
        verified_since=publisher.verified_since.isoformat() if publisher.verified_since else None,
        website_url=publisher.website_url,
        created_at=publisher.created_at.isoformat(),
        updated_at=publisher.updated_at.isoformat(),
    )


@router.put("/publishers/{publisher_id}", response_model=PublisherResponse)
async def admin_update_publisher(
    publisher_id: uuid.UUID,
    req: PublisherUpdateRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> PublisherResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update publishers",
        )

    updates: dict[str, object] = {k: v for k, v in req.model_dump().items() if v is not None}

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            if "name" in updates:
                name_val = updates["name"]
                if not isinstance(name_val, str):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="publisher_name_invalid: Name must be a string",
                    )
                existing = await get_publisher_by_name(session, current_user.organisation_id, name_val)
                if existing is not None and existing.id != publisher_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A publisher with this name already exists",
                    )

            if "public_key_hex" in updates:
                key_val = updates["public_key_hex"]
                if not isinstance(key_val, str):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="publisher_key_invalid: Public key must be a string",
                    )
                existing_key = await get_publisher_by_key(session, current_user.organisation_id, key_val)
                if existing_key is not None and existing_key.id != publisher_id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A publisher with this public key already exists",
                    )

            try:
                publisher = await crud_update_publisher(session, publisher_id, updates)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    if publisher is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Publisher not found",
        )

    return PublisherResponse(
        id=str(publisher.id),
        name=publisher.name,
        contact_email=publisher.contact_email,
        public_key_hex=publisher.public_key_hex,
        trust_tier=publisher.trust_tier,
        verified_since=publisher.verified_since.isoformat() if publisher.verified_since else None,
        website_url=publisher.website_url,
        created_at=publisher.created_at.isoformat(),
        updated_at=publisher.updated_at.isoformat(),
    )


@router.delete("/publishers/{publisher_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_publisher(
    publisher_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can delete publishers",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            deleted = await crud_delete_publisher(session, publisher_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Publisher not found",
        )


# ── Run Retention / Purge ──────────────────────────────────────────────


class RetentionPurgeRequest(BaseModel):
    max_age_days: int = 90


@router.post("/purge/runs", status_code=status.HTTP_200_OK)
async def admin_retention_purge_runs(
    req: RetentionPurgeRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can trigger run retention purge",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        deleted = await batch_delete_old_terminal_runs(session, max_age_days=req.max_age_days)

    return {"deleted_run_count": deleted}


class ManualPurgeRequest(BaseModel):
    older_than: str


@router.post("/purge", status_code=status.HTTP_200_OK)
async def admin_manual_purge(
    req: ManualPurgeRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can purge runs",
        )

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            result = await purge_runs(session, older_than=req.older_than)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="run_purge",
                actor_user_id=current_user.account_id,
                resource_type="run",
                payload_json={"older_than": req.older_than},
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )

    return result


class PurgeRunsRequest(BaseModel):
    older_than_days: int = 90


class PurgeRunsResponse(BaseModel):
    purged_count: int


@router.post("/runs/purge", status_code=status.HTTP_200_OK)
async def admin_purge_stale_runs(
    request: PurgeRunsRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> PurgeRunsResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can purge stale runs",
        )

    cutoff = datetime.now(UTC) - timedelta(days=request.older_than_days)
    terminal_states = ("complete", "failed", "eval_failed", "cancelled")

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        result = await session.execute(
            delete(Run)
            .where(
                Run.organisation_id == current_user.organisation_id,
                Run.status.in_(terminal_states),
                Run.created_at < cutoff,
            )
        )

    return PurgeRunsResponse(purged_count=result.rowcount)  # type: ignore[attr-defined]


# ── Run Retention ────────────────────────────────────────────────────────────


class RetentionConfigResponse(BaseModel):
    retention_days: int = 90


class UpdateRetentionRequest(BaseModel):
    retention_days: int = Field(default=90, ge=7, le=365)


class StorageInfoResponse(BaseModel):
    total_runs: int
    status_breakdown: dict[str, int]
    estimated_saved_bytes: int


class StatusCount(BaseModel):
    status: str
    count: int


@router.get("/runs/retention", response_model=RetentionConfigResponse)
async def admin_get_retention(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> RetentionConfigResponse:
    if current_user.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin users can view retention")
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        result = await session.execute(
            select(Organisation.settings_json).where(Organisation.id == current_user.organisation_id).limit(1)
        )
        row = result.scalar_one_or_none()
    retention_days = 90
    if row and isinstance(row, dict):
        retention_days = row.get("retention_days", 90)
    return RetentionConfigResponse(retention_days=retention_days)


@router.put("/runs/retention", status_code=status.HTTP_200_OK)
async def admin_update_retention(
    req: UpdateRetentionRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> RetentionConfigResponse:
    if current_user.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin users can update retention")
    logger.info(
        "run_retention.updated",
        extra={
            "org_id": str(current_user.organisation_id),
            "retention_days": req.retention_days,
        },
    )
    return RetentionConfigResponse(retention_days=req.retention_days)


@router.get("/runs/storage", response_model=StorageInfoResponse)
async def admin_get_storage(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> StorageInfoResponse:
    if current_user.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin users can view storage")
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        total = (
            await session.execute(
                select(func.count()).select_from(Run).where(Run.organisation_id == current_user.organisation_id)
            )
        ).scalar() or 0

        status_rows = (
            await session.execute(
                select(Run.status, func.count().label("cnt"))
                .where(Run.organisation_id == current_user.organisation_id)
                .group_by(Run.status)
            )
        ).all()

    breakdown: dict[str, int] = {}
    for row in status_rows:
        breakdown[row.status] = row.cnt

    terminal_states = ("complete", "failed", "eval_failed", "cancelled")
    terminal_count = sum(breakdown.get(s, 0) for s in terminal_states)
    estimated_saved_bytes = terminal_count * 4096

    return StorageInfoResponse(
        total_runs=total,
        status_breakdown=breakdown,
        estimated_saved_bytes=estimated_saved_bytes,
    )


# ── HITL Overdue Warning ────────────────────────────────────────────────────


class OverdueClaimItem(BaseModel):
    id: str
    pipeline_run_id: str
    node_id: str
    created_at: str
    age_hours: float
    status: str


class OverdueClaimsResponse(BaseModel):
    claims: list[OverdueClaimItem]


@router.get("/hitl/overdue", response_model=OverdueClaimsResponse)
async def admin_overdue_hitl_claims(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OverdueClaimsResponse:
    """List overdue HITL claims across the organisation."""
    if current_user.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        claims = await get_overdue_claims(session, current_user.organisation_id)

    return OverdueClaimsResponse(claims=[OverdueClaimItem(**c) for c in claims])
