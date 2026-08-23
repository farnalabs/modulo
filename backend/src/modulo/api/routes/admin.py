"""Admin-only routes for organisation, user, team, and billing management."""

import asyncio
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Date, case, cast, delete, func, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_RESOURCE_ALREADY_EXISTS,
    MSG_THIS_FEATURE_NOT_AVAILABLE,
    MSG_UNEXPECTED_ERROR,
)
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    deny_break_glass_mint,
    get_db_session,
    require_feature,
    require_permission,
    require_system_or_org_admin,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.core.audit_logger import append_audit_event
from modulo.core.eval_engine.okr import track_okr_progress
from modulo.core.eval_engine.regression import VALID_TRENDS, detect_regressions
from modulo.core.feature_flags import resolve_plan_context
from modulo.core.hitl_manager.overdue_warning import get_overdue_claims
from modulo.db.crud.account import get_account_by_email, get_account_by_id
from modulo.db.crud.eval_run import non_guardrail_eval_results_clause
from modulo.db.crud.last_admin_guard import (
    LastAdminLockoutError,
    LastAdminLockoutUnavailableError,
    assert_not_last_admin,
)
from modulo.db.crud.org_membership import create_membership, get_membership_by_account_and_org
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
from modulo.db.crud.run import (
    batch_delete_old_terminal_runs,
    get_org_run_concurrency_limit,
    get_sandbox_concurrency_limit,
    purge_runs,
)
from modulo.db.crud.team import (
    TeamUpdateOutcome,
    count_owned_resources,
    create_team,
    delete_team,
    get_team,
    get_team_by_name,
    list_teams,
    reassign_team_resources_to_org,
    update_team_if_unchanged,
)
from modulo.db.crud.team import update_team as crud_update_team
from modulo.db.crud.team_membership import list_team_memberships_for_account, remove_team_member
from modulo.db.crud.token_family import blacklist_family, list_families_for_account
from modulo.db.models.account import Account
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.publisher import Publisher
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

_CODE_ROUTES_ADMIN = "routes.admin"
_MSG_TEAM_NAME_ALREADY_EXISTS = "A team with this name already exists in your organisation"
_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE = "Database temporarily unavailable. Please try again."
_CODE_ADMIN_ADMIN_CREATE_TEAM = "admin.admin_create_team"
_MSG_ORGANISATION_NOT_FOUND = "Organisation not found"
_MSG_USER_NOT_FOUND = "User not found"
_MSG_USER_NOT_FOUND_IN_ORGANISATION = "User not found in this organisation"
_MSG_BREAK_GLASS_ACCOUNTS_CANNOT = "Break-glass accounts cannot be managed via the admin API"
_CODE_ADMIN_ADMIN_DEACTIVATE_USER = "admin.admin_deactivate_user"
_CODE_ADMIN_ADMIN_REACTIVATE_USER = "admin.admin_reactivate_user"
_CODE_ADMIN_ADMIN_UPDATE_TEAM = "admin.admin_update_team"
_CODE_ADMIN_ADMIN_DELETE_TEAM = "admin.admin_delete_team"
_CODE_ORG_DELETE = "org.delete"
_CODE_ADMIN_REQUEST_ORG_DELETION = "admin.request_org_deletion"
_CODE_ADMIN_CONFIRM_ORG_DELETION = "admin.confirm_org_deletion"
_CODE_ADMIN_CANCEL_ORG_DELETION = "admin.cancel_org_deletion"
_CODE_ADMIN_EXPORT_ORG_DATA = "admin.export_org_data"
_CODE_ADMIN_DELETE_ORG_IMMEDIATE = "admin.delete_org_immediate"
_MSG_DATABASE_ERROR_PLEASE_TRY = "Database error. Please try again later."
_RE_GREEN_OR_AMBER = "^(green|amber)$"
_MSG_DATABASE_ERROR_OCCURRED_PLEASE = "A database error occurred. Please try again later."
_MSG_TEAM_NOT_FOUND = "Team not found"


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ── Shared helpers ──────────────────────────────────────────────────────────


def _require_admin(current_user: TenantPrincipal, action: str) -> None:
    """Raise 403 unless the principal holds the admin role."""
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only admin users can {action}",
        )


def _raise_conflict() -> NoReturn:
    """Standard IntegrityError mapping: 409 resource-already-exists."""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=MSG_RESOURCE_ALREADY_EXISTS,
    ) from None


def _raise_feature_not_available() -> NoReturn:
    """Standard ProgrammingError mapping: 501 feature-not-available."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=MSG_FEATURE_NOT_AVAILABLE,
    ) from None


def _raise_this_feature_not_available() -> NoReturn:
    """ProgrammingError mapping using the ``This feature`` variant message."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
    ) from None


def _raise_db_temporarily_unavailable() -> NoReturn:
    """Standard SQLAlchemyError mapping: 503 database temporarily unavailable."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
    ) from None


def _raise_db_error_occurred() -> NoReturn:
    """SQLAlchemyError mapping: 503 with the generic database-error message."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
    ) from None


def _raise_db_unavailable(detail: str) -> NoReturn:
    """SQLAlchemyError mapping with a route-specific 503 message."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    ) from None


def _raise_unexpected(detail: str) -> NoReturn:
    """Generic catch-all mapping: 500 internal error."""
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=detail,
    ) from None


def _update_org_setting(org: Organisation, key: str, value: object) -> None:
    """Merge ``value`` into ``org.settings_json`` under ``key`` without dropping other keys."""
    settings = dict(org.settings_json) if org.settings_json else {}
    settings[key] = value
    org.settings_json = settings


def _to_user_list_item(account: Account, org_role: str) -> "UserListItem":
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


def _to_publisher_response(publisher: Publisher) -> "PublisherResponse":
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


class _TeamAuditSpec(NamedTuple):
    event_type: str
    payload: dict[str, object]
    log_code: str
    programming_warning: str
    sqlalchemy_warning: str


async def _append_team_audit_event(
    session: AsyncSession,
    current_user: TenantPrincipal,
    *,
    team_id: uuid.UUID,
    spec: _TeamAuditSpec,
) -> None:
    """Append a team audit event, degrading to a warning on DB failure.

    An ``IntegrityError`` still surfaces as 409; ``ProgrammingError`` and
    ``SQLAlchemyError`` are logged and swallowed so the preceding team mutation
    is never rolled back by a failed audit write.
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type=spec.event_type,
                actor_user_id=current_user.account_id,
                resource_type="team",
                resource_id=team_id,
                payload_json=spec.payload,
            )
    except IntegrityError:
        logger.exception(spec.log_code)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(spec.log_code)
        logger.warning(
            spec.programming_warning,
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
    except SQLAlchemyError:
        logger.exception(spec.log_code)
        logger.warning(
            spec.sqlalchemy_warning,
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )


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


class _SearchParams(NamedTuple):
    org_id: uuid.UUID
    like: str
    prefix: str
    limit: int
    offset: int


async def _search_pipelines(
    session: AsyncSession, params: _SearchParams
) -> tuple[list[tuple[int, SearchResultItem]], int]:
    rows = (
        await session.execute(
            text("""
                SELECT id, name, description,
                    CASE WHEN LOWER(name) LIKE LOWER(:prefix) THEN 2 ELSE 1 END AS relevance
                FROM pipelines
                WHERE organisation_id = :org_id
                    AND (LOWER(name) LIKE LOWER(:like) OR LOWER(description) LIKE LOWER(:like))
                ORDER BY relevance DESC, name ASC
                LIMIT :lim OFFSET :off
            """),
            {
                "org_id": params.org_id,
                "like": params.like,
                "prefix": params.prefix,
                "lim": params.limit,
                "off": params.offset,
            },
        )
    ).all()
    count = (
        await session.execute(
            text("""
                SELECT COUNT(*) FROM pipelines
                WHERE organisation_id = :org_id
                    AND (LOWER(name) LIKE LOWER(:like) OR LOWER(description) LIKE LOWER(:like))
            """),
            {"org_id": params.org_id, "like": params.like},
        )
    ).scalar() or 0

    items = [
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
        for row in rows
    ]
    return items, count


async def _search_runs(session: AsyncSession, params: _SearchParams) -> tuple[list[tuple[int, SearchResultItem]], int]:
    rows = (
        await session.execute(
            text("""
                SELECT r.id, r.run_number, CAST(r.id AS TEXT) AS display_id, p.name AS pipeline_name,
                    CASE WHEN LOWER(CAST(r.id AS TEXT)) LIKE LOWER(:prefix) THEN 2
                         WHEN LOWER(p.name) LIKE LOWER(:like) THEN 1 ELSE 0 END AS relevance
                FROM runs r
                JOIN pipelines p ON p.id = r.pipeline_id
                WHERE r.organisation_id = :org_id
                    AND (
                        LOWER(CAST(r.id AS TEXT)) LIKE LOWER(:prefix)
                        OR LOWER(p.name) LIKE LOWER(:like)
                    )
                ORDER BY relevance DESC, r.created_at DESC
                LIMIT :lim OFFSET :off
            """),
            {
                "org_id": params.org_id,
                "like": params.like,
                "prefix": params.prefix,
                "lim": params.limit,
                "off": params.offset,
            },
        )
    ).all()
    count = (
        await session.execute(
            text("""
                SELECT COUNT(*) FROM runs r
                JOIN pipelines p ON p.id = r.pipeline_id
                WHERE r.organisation_id = :org_id
                    AND (
                        LOWER(CAST(r.id AS TEXT)) LIKE LOWER(:prefix)
                        OR LOWER(p.name) LIKE LOWER(:like)
                    )
            """),
            {"org_id": params.org_id, "like": params.like, "prefix": params.prefix},
        )
    ).scalar() or 0

    items: list[tuple[int, SearchResultItem]] = []
    for row in rows:
        display_id = f"#{row.run_number}" if row.run_number is not None else f"#{str(row.id)[:8]}"
        items.append(
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
    return items, count


async def _search_audit(session: AsyncSession, params: _SearchParams) -> tuple[list[tuple[int, SearchResultItem]], int]:
    rows = (
        await session.execute(
            text("""
                SELECT id, event_type, resource_type,
                    CASE
                        WHEN LOWER(event_type) LIKE LOWER(:prefix) THEN 2
                        WHEN LOWER(event_type) LIKE LOWER(:like)
                             OR LOWER(resource_type) LIKE LOWER(:like)
                             OR LOWER(CAST(payload_json AS TEXT)) LIKE LOWER(:like) THEN 1
                        ELSE 0
                    END AS relevance
                FROM audit_events
                WHERE organisation_id = :org_id
                    AND (
                        LOWER(event_type) LIKE LOWER(:like)
                        OR LOWER(resource_type) LIKE LOWER(:like)
                        OR LOWER(CAST(payload_json AS TEXT)) LIKE LOWER(:like)
                    )
                ORDER BY relevance DESC, created_at DESC
                LIMIT :lim OFFSET :off
            """),
            {
                "org_id": params.org_id,
                "like": params.like,
                "prefix": params.prefix,
                "lim": params.limit,
                "off": params.offset,
            },
        )
    ).all()
    count = (
        await session.execute(
            text("""
                SELECT COUNT(*) FROM audit_events
                WHERE organisation_id = :org_id
                    AND (
                        LOWER(event_type) LIKE LOWER(:like)
                        OR LOWER(resource_type) LIKE LOWER(:like)
                        OR LOWER(CAST(payload_json AS TEXT)) LIKE LOWER(:like)
                    )
            """),
            {"org_id": params.org_id, "like": params.like},
        )
    ).scalar() or 0

    items: list[tuple[int, SearchResultItem]] = []
    for row in rows:
        title = row.event_type
        if row.resource_type:
            title = f"{row.event_type} — {row.resource_type}"
        items.append(
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
    return items, count


async def _search_library(
    session: AsyncSession, params: _SearchParams
) -> tuple[list[tuple[int, SearchResultItem]], int]:
    rows = (
        await session.execute(
            text("""
                SELECT id, name, description,
                    CASE WHEN LOWER(name) LIKE LOWER(:prefix) THEN 2 ELSE 1 END AS relevance
                FROM library_primitives
                WHERE organisation_id = :org_id
                    AND (LOWER(name) LIKE LOWER(:like) OR LOWER(description) LIKE LOWER(:like))
                ORDER BY relevance DESC, name ASC
                LIMIT :lim OFFSET :off
            """),
            {
                "org_id": params.org_id,
                "like": params.like,
                "prefix": params.prefix,
                "lim": params.limit,
                "off": params.offset,
            },
        )
    ).all()
    count = (
        await session.execute(
            text("""
                SELECT COUNT(*) FROM library_primitives
                WHERE organisation_id = :org_id
                    AND (LOWER(name) LIKE LOWER(:like) OR LOWER(description) LIKE LOWER(:like))
            """),
            {"org_id": params.org_id, "like": params.like},
        )
    ).scalar() or 0

    items = [
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
        for row in rows
    ]
    return items, count


@router.get("/search")
@handle_db_errors("admin.global_search")
async def global_search(
    q: str = Query(min_length=1),
    type_filter: str = Query(default="all", alias="type", pattern=r"^(all|pipeline|run|audit|library)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    if current_user.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            params = _SearchParams(
                org_id=current_user.organisation_id,
                like=f"%{q}%",
                prefix=f"{q}%",
                limit=limit,
                offset=offset,
            )

            search_types: list[str] = ["pipeline", "run", "audit", "library"] if type_filter == "all" else [type_filter]

            all_items: list[tuple[int, SearchResultItem]] = []
            total_by_type: dict[str, int] = {"pipeline": 0, "run": 0, "audit": 0, "library": 0}

            if "pipeline" in search_types:
                items, count = await _search_pipelines(session, params)
                all_items.extend(items)
                total_by_type["pipeline"] = count
            if "run" in search_types:
                items, count = await _search_runs(session, params)
                all_items.extend(items)
                total_by_type["run"] = count
            if "audit" in search_types:
                items, count = await _search_audit(session, params)
                all_items.extend(items)
                total_by_type["audit"] = count
            if "library" in search_types:
                items, count = await _search_library(session, params)
                all_items.extend(items)
                total_by_type["library"] = count

            all_items.sort(key=lambda x: (-x[0], x[1].title))
            paginated = [item for _, item in all_items[offset : offset + limit]]

    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

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


@router.post("/users", status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.admin_create_user")
async def admin_create_user(
    req: CreateUserRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> CreateUserResponse:
    _require_admin(current_user, "create users")

    if req.org_role not in ("admin", "operator", "runner", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"Invalid role: {req.org_role}. Must be one of: admin, operator, runner, viewer"),
        )

    try:
        async with session.begin():
            existing = await get_account_by_email(session, req.email)
            if existing is not None:
                membership = await get_membership_by_account_and_org(session, existing.id, current_user.organisation_id)
                if membership is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A user with this email already exists in this organisation",
                    )
                # SECURITY (#1185): refuse password hash overwrite when the
                # account belongs to other orgs — prevents cross-tenant takeover.
                # Allow adoption for SSO/SCIM accounts (no local password).
                if existing.password_hash is not None and existing.auth_provider == "local":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "EMAIL_ACCOUNT_EXISTS: An account with this email exists"
                            " in another organisation. Password-based adoption is not allowed."
                        ),
                    )

        try:
            validate_password_strength(req.password)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

        pw_hash = hash_password(req.password)

        async with session.begin():
            from modulo.db.crud.account import create_account

            if existing is not None:
                account = existing
                # SECURITY (#1185): only allow password hash overwrite for
                # accounts that have NO existing password (SSO/SCIM JIT).
                if account.password_hash is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "EMAIL_ACCOUNT_EXISTS: An account with this email exists"
                            " in another organisation. Password-based adoption is not allowed."
                        ),
                    )
                account.password_hash = pw_hash
            else:
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
    except ProgrammingError:
        logger.warning("admin_create_user: DB migration may be missing", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Database migration incomplete. Please run database migrations.",
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_create_user: DB error")
        _raise_db_unavailable("Database error occurred. Please try again later.")


class AdminCreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


class AdminUpdateTeamRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    expected_updated_at: str | None = None


class AdminCreateTeamResponse(BaseModel):
    id: str
    name: str
    description: str | None
    account_id: str
    created_at: str


@router.post(
    "/teams",
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_feature("team_rbac")],
)
async def admin_create_team(
    req: AdminCreateTeamRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminCreateTeamResponse:
    _require_admin(current_user, "create teams")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            existing = await get_team_by_name(session, current_user.organisation_id, req.name)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_MSG_TEAM_NAME_ALREADY_EXISTS,
                )
            team = await create_team(
                session,
                org_id=current_user.organisation_id,
                name=req.name,
                account_id=current_user.account_id,
                description=req.description,
            )
    except HTTPException:
        raise
    except IntegrityError:
        logger.exception("admin_create_team IntegrityError", extra={"org_id": str(current_user.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_TEAM_NAME_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception("admin_create_team SQLAlchemyError", extra={"org_id": str(current_user.organisation_id)})
        _raise_db_temporarily_unavailable()
    except Exception:
        logger.exception("admin_create_team unexpected error", extra={"org_id": str(current_user.organisation_id)})
        _raise_unexpected("An unexpected error occurred while creating the team.")

    await _append_team_audit_event(
        session,
        current_user,
        team_id=team.id,
        spec=_TeamAuditSpec(
            event_type="team_created",
            payload={"team_id": str(team.id), "name": team.name},
            log_code=_CODE_ADMIN_ADMIN_CREATE_TEAM,
            programming_warning="admin_create_team audit event ProgrammingError — team was created",
            sqlalchemy_warning="admin_create_team audit event SQLAlchemyError — team was created",
        ),
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
    plan_id: str | None = None


class OrgProfileResponse(BaseModel):
    id: str
    name: str
    slug: str
    logo_url: str | None = None
    plan_id: str | None = None
    created_at: str


@router.get("/org")
@handle_db_errors("admin.admin_get_org")
async def admin_get_org(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgProfileResponse:
    _require_admin(current_user, "view org profile")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_ORGANISATION_NOT_FOUND,
                )
    except IntegrityError:
        logger.exception("admin_get_org IntegrityError", extra={"org_id": str(current_user.organisation_id)})
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin_get_org ProgrammingError", extra={"org_id": str(current_user.organisation_id)})
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception("admin_get_org SQLAlchemyError", extra={"org_id": str(current_user.organisation_id)})
        _raise_db_unavailable("Database error while fetching org profile.")

    current_settings = org.settings_json or {}
    return OrgProfileResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        logo_url=current_settings.get("logo_url"),
        plan_id=org.plan_id,
        created_at=org.created_at.isoformat(),
    )


@router.put("/org")
@handle_db_errors("admin.admin_update_org")
async def admin_update_org(
    req: UpdateOrgRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgProfileResponse:
    _require_admin(current_user, "update org profile")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_ORGANISATION_NOT_FOUND,
                )

            updates: dict[str, object] = {}
            if req.name is not None:
                updates["name"] = req.name
            if req.logo_url is not None:
                existing_settings = dict(org.settings_json or {})
                existing_settings["logo_url"] = req.logo_url
                updates["settings_json"] = existing_settings
            if req.plan_id is not None:
                updates["plan_id"] = req.plan_id

            if updates:
                updated = await update_organisation(session, current_user.organisation_id, updates)
                if updated is not None:
                    org = updated
    except IntegrityError:
        logger.exception("admin_update_org IntegrityError", extra={"org_id": str(current_user.organisation_id)})
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin_update_org ProgrammingError", extra={"org_id": str(current_user.organisation_id)})
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception("admin_update_org SQLAlchemyError", extra={"org_id": str(current_user.organisation_id)})
        _raise_db_unavailable("Database error while updating org profile.")

    current_settings = org.settings_json or {}
    return OrgProfileResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        logo_url=current_settings.get("logo_url"),
        plan_id=org.plan_id,
        created_at=org.created_at.isoformat(),
    )


@router.post("/org/regenerate-api-key", status_code=status.HTTP_200_OK, dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("admin.admin_regenerate_api_key")
async def admin_regenerate_api_key(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    _require_admin(current_user, "regenerate API key")

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
    except IntegrityError:
        logger.exception("admin_regenerate_api_key IntegrityError", extra={"org_id": str(current_user.organisation_id)})
        _raise_conflict()
    except ProgrammingError:
        logger.exception(
            "admin_regenerate_api_key ProgrammingError",
            extra={"org_id": str(current_user.organisation_id)},
        )
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(
            "admin_regenerate_api_key SQLAlchemyError",
            extra={"org_id": str(current_user.organisation_id)},
        )
        _raise_db_unavailable("Database error while regenerating API key.")

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


@router.get("/users")
@handle_db_errors("admin.admin_list_users")
async def admin_list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=1000),
    search: str | None = Query(None, min_length=1),
    role: str | None = Query(None, pattern=r"^(admin|operator|runner|viewer)$"),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListResponse:
    _require_admin(current_user, "list users")

    try:
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

    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_this_feature_not_available()

    return UserListResponse(
        items=[_to_user_list_item(a, m.role) for a, m in accounts_memberships],
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
) -> tuple[list[tuple[Account, OrgMembership]], int]:
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
    return [(row[0], row[1]) for row in result.all()], total


class UpdateUserRequest(BaseModel):
    org_role: str | None = Field(None, pattern=r"^(admin|operator|runner|viewer)$")
    is_active: bool | None = None


@router.put("/users/{user_id}")
@handle_db_errors("admin.admin_update_user")
async def admin_update_user(
    user_id: uuid.UUID,
    req: UpdateUserRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListItem:
    _require_admin(current_user, "update users")

    if req.is_active is False and user_id == current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Cannot deactivate yourself",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            # SECURITY (#1188): verify the target has membership in the caller's org
            # before allowing any mutation — prevents cross-tenant account interference.
            target_membership = await get_membership_by_account_and_org(session, user_id, current_user.organisation_id)
            if target_membership is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_USER_NOT_FOUND_IN_ORGANISATION,
                )

            target_role_after = req.org_role
            if target_role_after is None:
                target_role_after = target_membership.role

            await assert_not_last_admin(
                session,
                org_id=current_user.organisation_id,
                target_account_id=user_id,
                target_role_after=target_role_after,
                target_active_after=req.is_active,
            )

            account = await get_account_by_id(session, user_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_USER_NOT_FOUND)

            if account.is_break_glass:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=_MSG_BREAK_GLASS_ACCOUNTS_CANNOT,
                )

            if req.is_active is not None:
                account.active = req.is_active
            if req.is_active is True:
                from sqlalchemy import update as sa_update

                await session.execute(
                    sa_update(OrgMembership)
                    .where(
                        OrgMembership.account_id == user_id,
                        OrgMembership.organisation_id == current_user.organisation_id,
                    )
                    .values(deactivated_at=None)
                )
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

    except LastAdminLockoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.reason,
        ) from None
    except LastAdminLockoutUnavailableError:
        logger.exception("routes.admin.last_admin_guard_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify the last-admin invariant. Please try again.",
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_this_feature_not_available()

    org_role = req.org_role or (await _get_org_role(session, user_id, current_user.organisation_id))
    return _to_user_list_item(account, org_role)


async def _get_org_role(session: AsyncSession, account_id: uuid.UUID, org_id: uuid.UUID) -> str:
    membership = await get_membership_by_account_and_org(session, account_id, org_id)
    return membership.role if membership is not None else ""


def _extract_bg_pgcode(exc: BaseException) -> str | None:
    """Extract the SECURITY DEFINER custom ERRCODE (M2010/M2020/M2040)."""
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode is not None:
        return str(pgcode)
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate is not None:
        return str(sqlstate)
    return None


def _raise_bg_pgcode(
    exc: BaseException,
    *,
    unauthorized_status: int,
    conflict_status: int,
    not_found_status: int,
) -> None:
    """Map the SECURITY DEFINER's custom pgcodes to HTTP statuses.

    M2010 = caller not authorized, M2020 = would orphan org (last admin),
    M2040 = target does not exist. Raises HTTPException for a matching pgcode,
    otherwise returns so the caller's generic SQLAlchemyError handling (503)
    takes over. Called INSIDE the route's ``except SQLAlchemyError`` BEFORE the
    generic 503 mapping.
    """
    pgcode = _extract_bg_pgcode(exc)
    if pgcode == "M2010":
        raise HTTPException(
            status_code=unauthorized_status,
            detail="Caller is not authorized to deactivate this user",
        ) from None
    if pgcode == "M2020":
        raise HTTPException(
            status_code=conflict_status,
            detail="Cannot deactivate the last admin. Promote another user to admin first.",
        ) from None
    if pgcode == "M2040":
        raise HTTPException(
            status_code=not_found_status,
            detail=_MSG_USER_NOT_FOUND,
        ) from None


@router.post("/users/{user_id}/deactivate")
@handle_db_errors(_CODE_ADMIN_ADMIN_DEACTIVATE_USER)
async def admin_deactivate_user(
    user_id: uuid.UUID,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListItem:
    _require_admin(current_user, "deactivate users")

    if current_user.account_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
                    detail=_MSG_USER_NOT_FOUND,
                )

            if account.is_break_glass:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Break-glass accounts cannot be deactivated via the admin API",
                )

            membership = await get_membership_by_account_and_org(session, user_id, current_user.organisation_id)
            if membership is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_USER_NOT_FOUND_IN_ORGANISATION,
                )

            await assert_not_last_admin(
                session,
                org_id=current_user.organisation_id,
                target_account_id=user_id,
                target_role_after=None,
                target_active_after=False,
            )

            # Caller-bound SECURITY DEFINER: scoped family/key/membership
            # revocation + account-global active=false + per-org last-admin
            # M2020 + bg-only destructive tombstone. Atomic single statement.
            await session.execute(
                text("SELECT public.deactivate_break_glass(:caller, :target, false)"),
                {"caller": current_user.account_id, "target": user_id},
            )
            await session.refresh(account)

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

            org_role = await _get_org_role(session, user_id, current_user.organisation_id)
    except LastAdminLockoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.reason,
        ) from None
    except LastAdminLockoutUnavailableError:
        logger.exception("admin.admin_deactivate_user.last_admin_guard_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify the last-admin invariant. Please try again.",
        ) from None
    except IntegrityError:
        logger.exception(_CODE_ADMIN_ADMIN_DEACTIVATE_USER)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_ADMIN_DEACTIVATE_USER)
        _raise_feature_not_available()
    except SQLAlchemyError as exc:
        logger.exception(_CODE_ADMIN_ADMIN_DEACTIVATE_USER)
        logger.warning(
            "admin_deactivate_user SQLAlchemyError",
            extra={"org_id": str(current_user.organisation_id), "user_id": str(user_id)},
        )
        _raise_bg_pgcode(
            exc,
            unauthorized_status=status.HTTP_403_FORBIDDEN,
            conflict_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            not_found_status=status.HTTP_404_NOT_FOUND,
        )
        _raise_db_temporarily_unavailable()
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "admin_deactivate_user unexpected error",
            extra={"org_id": str(current_user.organisation_id), "user_id": str(user_id)},
        )
        _raise_unexpected("An unexpected error occurred while deactivating the user.")

    return _to_user_list_item(account, org_role)


@router.post("/users/{user_id}/reactivate")
@handle_db_errors(_CODE_ADMIN_ADMIN_REACTIVATE_USER)
async def admin_reactivate_user(
    user_id: uuid.UUID,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserListItem:
    _require_admin(current_user, "reactivate users")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            account = await get_account_by_id(session, user_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_USER_NOT_FOUND)

            if account.is_break_glass:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=_MSG_BREAK_GLASS_ACCOUNTS_CANNOT,
                )

            # SECURITY (#1188): verify the target has membership in the caller's org
            # before reactivating — prevents cross-tenant account interference.
            membership = await get_membership_by_account_and_org(session, user_id, current_user.organisation_id)
            if membership is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_USER_NOT_FOUND_IN_ORGANISATION,
                )

            account.active = True

            from sqlalchemy import update as sa_update

            await session.execute(
                sa_update(OrgMembership)
                .where(
                    OrgMembership.account_id == user_id,
                    OrgMembership.organisation_id == current_user.organisation_id,
                )
                .values(deactivated_at=None)
            )

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

            org_role = await _get_org_role(session, user_id, current_user.organisation_id)
    except IntegrityError:
        logger.exception(_CODE_ADMIN_ADMIN_REACTIVATE_USER)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_ADMIN_REACTIVATE_USER)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_ADMIN_REACTIVATE_USER)
        logger.warning(
            "admin_reactivate_user SQLAlchemyError",
            extra={"org_id": str(current_user.organisation_id), "user_id": str(user_id)},
        )
        _raise_db_temporarily_unavailable()
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "admin_reactivate_user unexpected error",
            extra={"org_id": str(current_user.organisation_id), "user_id": str(user_id)},
        )
        _raise_unexpected("An unexpected error occurred while reactivating the user.")

    return _to_user_list_item(account, org_role)


class AdminResetPasswordResponse(BaseModel):
    temporary_password: str


@router.post("/users/{user_id}/reset-password")
@handle_db_errors("admin.admin_reset_password")
async def admin_reset_password(
    user_id: uuid.UUID,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminResetPasswordResponse:
    _require_admin(current_user, "reset passwords")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            account = await get_account_by_id(session, user_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_USER_NOT_FOUND)

            if account.is_break_glass:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=_MSG_BREAK_GLASS_ACCOUNTS_CANNOT,
                )

            # SECURITY (#1186): verify the target is a member of the caller's org
            # before resetting password — prevents cross-tenant credential takeover.
            membership = await get_membership_by_account_and_org(session, user_id, current_user.organisation_id)
            if membership is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_USER_NOT_FOUND_IN_ORGANISATION,
                )

            temporary_password = secrets.token_urlsafe(18)[:24]
            account.password_hash = hash_password(temporary_password)

            families = await list_families_for_account(session, user_id)
            for family in families:
                await blacklist_family(session, family.family_id, user_id)

            await session.flush()

    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_this_feature_not_available()

    return AdminResetPasswordResponse(temporary_password=temporary_password)


# ── Team Management ──────────────────────────────────────────


class AdminTeamItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    account_id: str
    member_count: int = 0
    owned_resource_count: int = 0
    created_at: str
    updated_at: str = ""


class AdminTeamListResponse(BaseModel):
    items: list[AdminTeamItem]
    total: int
    page: int
    page_size: int


@router.get("/teams", dependencies=[require_feature("team_rbac")])
async def admin_list_teams(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=1000),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminTeamListResponse:
    _require_admin(current_user, "list teams")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            org_id = current_user.organisation_id
            result = await list_teams(session, org_id=org_id, page=page, page_size=page_size)

            # Enrich with member counts via ORM (avoids raw SQL type binding issues)
            team_ids = [t.id for t in result.items if t is not None]
            member_counts: dict[uuid.UUID, int] = {}
            if team_ids:
                count_rows = (
                    await session.execute(
                        select(TeamMembership.team_id, func.count().label("cnt"))
                        .where(TeamMembership.team_id.in_(team_ids))
                        .group_by(TeamMembership.team_id)
                    )
                ).all()
                member_counts.update({row.team_id: row.cnt for row in count_rows if row.team_id is not None})

            # Enrich with owned resource counts (4-way delete-blocking set)
            owned_resource_counts = await count_owned_resources(session, team_ids=team_ids)
    except IntegrityError:
        logger.exception("admin_list_teams IntegrityError", extra={"org_id": str(current_user.organisation_id)})
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin_list_teams ProgrammingError", extra={"org_id": str(current_user.organisation_id)})
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception("admin_list_teams SQLAlchemyError", extra={"org_id": str(current_user.organisation_id)})
        _raise_db_temporarily_unavailable()
    except HTTPException:
        raise
    except Exception:
        logger.exception("admin_list_teams unexpected error", extra={"org_id": str(current_user.organisation_id)})
        _raise_unexpected("An unexpected error occurred while fetching teams.")

    return AdminTeamListResponse(
        items=[
            AdminTeamItem(
                id=str(t.id),
                name=t.name,
                description=t.description,
                account_id=str(t.account_id),
                member_count=member_counts.get(t.id, 0),
                owned_resource_count=owned_resource_counts.get(t.id, 0),
                created_at=t.created_at.isoformat() if t.created_at else "",
                updated_at=t.updated_at.isoformat() if isinstance(t.updated_at, datetime) else "",
            )
            for t in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


async def _update_team_or_raise(
    session: AsyncSession,
    current_user: TenantPrincipal,
    team_id: uuid.UUID,
    updates: dict[str, Any],
    expected_updated_at: str | None,
) -> Any:
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.account_id, current_user.org_role)

        if "name" in updates:
            existing = await get_team_by_name(session, current_user.organisation_id, updates["name"])
            if existing is not None and existing.id != team_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_MSG_TEAM_NAME_ALREADY_EXISTS,
                )

        if expected_updated_at is not None:
            outcome, team = await update_team_if_unchanged(
                session,
                team_id,
                updates,
                expected_updated_at,
            )
            if outcome is TeamUpdateOutcome.NOT_FOUND:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)
            if outcome is TeamUpdateOutcome.STALE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=("Team was modified by another request. Refresh and try again (optimistic lock mismatch)."),
                )
        else:
            team = await crud_update_team(session, team_id, updates)
    return team


@router.put("/teams/{team_id}", dependencies=[require_feature("team_rbac")])
async def admin_update_team(
    team_id: uuid.UUID,
    req: AdminUpdateTeamRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminTeamItem:
    _require_admin(current_user, "update teams")

    updates = req.model_dump(exclude_unset=True)
    updates.pop("expected_updated_at", None)

    try:
        team = await _update_team_or_raise(session, current_user, team_id, updates, req.expected_updated_at)
    except IntegrityError:
        logger.exception(_CODE_ADMIN_ADMIN_UPDATE_TEAM)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_ADMIN_UPDATE_TEAM)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(
            "admin_update_team SQLAlchemyError",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        _raise_db_temporarily_unavailable()
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "admin_update_team unexpected error",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        _raise_unexpected("An unexpected error occurred while updating the team.")

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

    await _append_team_audit_event(
        session,
        current_user,
        team_id=team_id,
        spec=_TeamAuditSpec(
            event_type="team_updated",
            payload={"team_id": str(team_id), "updates": updates},
            log_code=_CODE_ADMIN_ADMIN_UPDATE_TEAM,
            programming_warning="admin_update_team audit event ProgrammingError — team was updated",
            sqlalchemy_warning="admin_update_team audit event SQLAlchemyError — team was updated",
        ),
    )

    return AdminTeamItem(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat(),
        updated_at=team.updated_at.isoformat() if isinstance(team.updated_at, datetime) else "",
    )


class BulkReassignResponse(BaseModel):
    reassigned: int
    resource_types: list[str] = Field(default_factory=list)


@router.post(
    "/teams/{team_id}/reassign-all",
    dependencies=[require_feature("team_rbac")],
)
@handle_db_errors("admin.reassign_all_team_resources")
async def admin_reassign_all_team_resources(
    team_id: uuid.UUID,
    current_user: TenantPrincipal = require_permission("team.delete"),
    session: AsyncSession = Depends(get_db_session),
) -> BulkReassignResponse:
    """Bulk-reassign every resource owned by ``team_id`` to org-wide.

    PRD §9.3 team-deletion flow: before deleting a team, the admin reassigns
    all team-owned resources to org-wide (``owner_team_id -> NULL``,
    ``visibility -> 'org'``), after which deletion is no longer blocked by
    ``team_has_resources``. Idempotent: a team with no owned resources returns
    ``reassigned=0``; reassigning already-org resources succeeds.
    """
    _require_admin(current_user, "reassign team resources")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            team = await get_team(session, team_id)
            if team is None or team.organisation_id != current_user.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

            reassigned, touched = await reassign_team_resources_to_org(
                session,
                org_id=current_user.organisation_id,
                team_id=team_id,
            )
    except IntegrityError:
        logger.exception("admin.admin_reassign_all_team_resources")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.admin_reassign_all_team_resources")
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(
            "admin_reassign_all_team_resources SQLAlchemyError",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        _raise_db_temporarily_unavailable()
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "admin_reassign_all_team_resources unexpected error",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        _raise_unexpected("An unexpected error occurred while reassigning team resources.")

    return BulkReassignResponse(reassigned=reassigned, resource_types=touched)


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[require_feature("team_rbac")])
async def admin_delete_team(
    team_id: uuid.UUID,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user, "delete teams")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            resource_checks: list[tuple[str, int]] = []
            for model_cls, label in [
                (Pipeline, "pipeline"),
                (ConnectorInstance, "connector"),
                (ModelBackend, "model backend"),
                (LibraryPrimitive, "library primitive"),
            ]:
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(model_cls)
                        .where(model_cls.__table__.c.owner_team_id == team_id)
                    )
                ).scalar() or 0
                if count > 0:
                    resource_checks.append((label, count))

            if resource_checks:
                details = "; ".join(f"{count} {label}(s)" for label, count in resource_checks)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"team_has_resources: Cannot delete team: still has resources — {details}",
                )

            deleted = await delete_team(session, team_id)
    except IntegrityError:
        logger.exception(_CODE_ADMIN_ADMIN_DELETE_TEAM)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_ADMIN_DELETE_TEAM)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(
            "admin_delete_team SQLAlchemyError",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        _raise_db_temporarily_unavailable()
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "admin_delete_team unexpected error",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        _raise_unexpected("An unexpected error occurred while deleting the team.")

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="team_deleted",
                actor_user_id=current_user.account_id,
                resource_type="team",
                resource_id=team_id,
                payload_json={"team_id": str(team_id)},
            )
    except IntegrityError:
        logger.exception(_CODE_ADMIN_ADMIN_DELETE_TEAM)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_ADMIN_DELETE_TEAM)
        logger.warning("Failed to record team_deleted audit event for team %s", team_id)


# ── Dashboard Summary Alias ──────────────────────────────────


@router.get("/dashboard/summary")
@handle_db_errors("admin.dashboard_summary")
async def admin_dashboard_summary(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    from modulo.api.routes.dashboard import dashboard_summary as _dashboard_summary

    return await _dashboard_summary(session=session, principal=current_user)


# ── SAQ Queue Metrics (API-only, plan F7) ─────────────────────────────────


class QueueMetricsResponse(BaseModel):
    queues: dict[str, int]


@router.get("/queues/metrics")
@handle_db_errors("admin.queue_metrics")
async def admin_queue_metrics(
    current_user: TenantPrincipal = require_permission("admin.queue_metrics"),
) -> QueueMetricsResponse:
    """LLEN of both configured SAQ queues (runs + system), PREFIX-AWARE.

    Queue names derive from ``SAQ_RUNS_QUEUE`` (``runs`` or ``staging-runs``);
    the system queue is derived the same way the workers derive it. API-only —
    no frontend card in this PR.
    """
    import contextlib

    import redis.asyncio as aioredis

    from modulo.settings import get_settings

    settings = get_settings()
    runs_queue = settings.saq_runs_queue
    system_queue = runs_queue.replace("runs", "system") if "runs" in runs_queue else "system"
    queues: dict[str, int] = {runs_queue: 0, system_queue: 0}
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        for qname in queues:
            try:
                # LLEN via execute_command — redis stubs type llen() as a
                # non-awaitable union, which breaks strict mypy.
                val = await r.execute_command("LLEN", f"saq:{qname}:queued")
                queues[qname] = int(val or 0)
            except Exception:
                logger.warning("admin.queue_metrics.llen_failed queue=%s", qname)
    except Exception as exc:
        logger.warning("admin.queue_metrics.redis_failed: %s", exc)
        err = HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis unavailable for queue metrics",
        )
        raise err from exc
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()
    return QueueMetricsResponse(queues=queues)


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


@router.get("/billing/overview")
@handle_db_errors("admin.admin_billing_overview")
async def admin_billing_overview(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> BillingOverviewResponse:
    _require_admin(current_user, "view billing")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_ORGANISATION_NOT_FOUND,
                )

            org_id = current_user.organisation_id
            user_count = (
                await session.execute(
                    select(func.count()).select_from(OrgMembership).where(OrgMembership.organisation_id == org_id)
                )
            ).scalar() or 0

            team_count = (
                await session.execute(select(func.count()).select_from(Team).where(Team.organisation_id == org_id))
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
        _raise_db_unavailable("Billing overview is temporarily unavailable.")

    plan_id = org.plan_id or "community"
    plan_context = await resolve_plan_context(settings, session, org)
    plan_tier = plan_context.tier()

    org_settings = org.settings_json or {}
    return BillingOverviewResponse(
        plan_id=plan_id,
        plan_tier=plan_tier,
        daily_spend_limit=float(org.daily_spend_limit) if org.daily_spend_limit else None,
        total_users=user_count,
        total_teams=team_count,
        total_pipelines=pipeline_count,
        total_runs_this_month=runs_this_month,
        license_key=org_settings.get("license_key"),
    )


# ── Org Deletion ─────────────────────────────────────────────────────


class DeletionRequestResponse(BaseModel):
    message: str
    token: str
    token_expires_at: str
    export_summary: dict[str, object]


@router.post(
    "/org/deletion-request",
    status_code=status.HTTP_202_ACCEPTED,
)
@handle_db_errors(_CODE_ADMIN_REQUEST_ORG_DELETION)
async def request_org_deletion(
    current_user: TenantPrincipal = require_system_or_org_admin(_CODE_ORG_DELETE),
    session: AsyncSession = Depends(get_db_session),
) -> DeletionRequestResponse:
    from modulo.core.audit_logger import append_audit_event
    from modulo.db.crud.org_deletion import request_org_deletion as _request_deletion

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

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
    except IntegrityError:
        logger.exception(_CODE_ADMIN_REQUEST_ORG_DELETION)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_REQUEST_ORG_DELETION)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_REQUEST_ORG_DELETION)
        _raise_db_unavailable("Database error while requesting org deletion.")

    export = result["export"]
    return DeletionRequestResponse(
        message="Deletion requested. A confirmation link has been generated (valid for 24 h).",
        token=result["token"],
        token_expires_at=result["token_expires_at"],
        export_summary={
            "organisation": export.get("organisation", [{}])[0].get("name", "unknown"),
            "user_count": len(export.get("memberships", [])),
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
    # B7 admin force — destructive. Proceeds despite live (non-terminal) runs.
    force: bool = False


class ConfirmDeletionResponse(BaseModel):
    message: str
    deleted_organisation_id: str
    hard_deleted_runs: int


@router.post("/org/deletion-confirm")
@handle_db_errors(_CODE_ADMIN_CONFIRM_ORG_DELETION)
async def confirm_org_deletion(
    req: ConfirmDeletionRequest,
    current_user: TenantPrincipal = require_system_or_org_admin(_CODE_ORG_DELETE),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmDeletionResponse:
    from modulo.db.crud.org_deletion import confirm_org_deletion as _confirm_deletion

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            try:
                result = await _confirm_deletion(
                    session,
                    org_id=current_user.organisation_id,
                    token=req.token,
                    immediate=False,
                    force=req.force,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError:
        logger.exception(_CODE_ADMIN_CONFIRM_ORG_DELETION)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_CONFIRM_ORG_DELETION)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_CONFIRM_ORG_DELETION)
        _raise_db_unavailable("Database error while confirming org deletion.")

    return ConfirmDeletionResponse(
        message=(
            "Organisation has been permanently deleted."
            + (" (FORCED — deleted despite live runs.)" if req.force else "")
        ),
        deleted_organisation_id=result["deleted_organisation_id"],
        hard_deleted_runs=result["hard_deleted_runs"],
    )


class CancelDeletionResponse(BaseModel):
    status: str


class OrgExportResponse(BaseModel):
    organisation: dict[str, object]
    exported_at: str


@router.patch("/org/deletion-cancel")
@handle_db_errors(_CODE_ADMIN_CANCEL_ORG_DELETION)
async def cancel_org_deletion(
    current_user: TenantPrincipal = require_system_or_org_admin(_CODE_ORG_DELETE),
    session: AsyncSession = Depends(get_db_session),
) -> CancelDeletionResponse:
    from modulo.db.crud.org_deletion import cancel_org_deletion as _cancel

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            try:
                result = await _cancel(session, org_id=current_user.organisation_id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError:
        logger.exception(_CODE_ADMIN_CANCEL_ORG_DELETION)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_CANCEL_ORG_DELETION)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_CANCEL_ORG_DELETION)
        _raise_db_unavailable("Database error while cancelling org deletion.")

    return CancelDeletionResponse(**result)


@router.get("/org/export")
@handle_db_errors(_CODE_ADMIN_EXPORT_ORG_DATA)
async def export_org_data(
    current_user: TenantPrincipal = require_system_or_org_admin(_CODE_ORG_DELETE),
    session: AsyncSession = Depends(get_db_session),
) -> OrgExportResponse:
    from modulo.db.crud.org_deletion import export_org_data as _export

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            try:
                bundle = await _export(session, org_id=current_user.organisation_id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IntegrityError:
        logger.exception(_CODE_ADMIN_EXPORT_ORG_DATA)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_EXPORT_ORG_DATA)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_EXPORT_ORG_DATA)
        _raise_db_unavailable("Database error while exporting org data.")

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


@router.delete("/org")
@handle_db_errors(_CODE_ADMIN_DELETE_ORG_IMMEDIATE)
async def delete_org_immediate(
    current_user: TenantPrincipal = require_system_or_org_admin(_CODE_ORG_DELETE),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmDeletionResponse:
    from modulo.core.audit_logger import append_audit_event
    from modulo.db.crud.org_deletion import confirm_org_deletion as _confirm_deletion
    from modulo.db.crud.org_deletion import request_org_deletion as _request_deletion

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

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
                force=True,
            )
    except IntegrityError:
        logger.exception(_CODE_ADMIN_DELETE_ORG_IMMEDIATE)
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_DELETE_ORG_IMMEDIATE)
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_DELETE_ORG_IMMEDIATE)
        _raise_db_unavailable("Database error while deleting org.")

    return ConfirmDeletionResponse(
        message="Organisation has been permanently deleted. (FORCED — deleted despite live runs.)",
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


async def _eval_summary(session: AsyncSession) -> EvalDashboardSummary:
    summary_q = select(
        func.count(EvalResult.id).label("total_results"),
        func.sum(case((EvalResult.passed, 1), else_=0)).label("passed"),
        func.sum(case((EvalResult.passed.is_(False), 1), else_=0)).label("failed"),
    ).where(non_guardrail_eval_results_clause())
    summary_row = (await session.execute(summary_q)).one()

    defs_q = select(func.count(EvalDefinition.id)).select_from(EvalDefinition)
    total_defs = (await session.execute(defs_q)).scalar() or 0

    total_results = summary_row.total_results or 0
    passed = summary_row.passed or 0
    failed = summary_row.failed or 0
    pass_rate = round(passed / total_results, 4) if total_results > 0 else 0.0

    return EvalDashboardSummary(
        total_results=total_results,
        passed=passed,
        failed=failed,
        pass_rate=pass_rate,
        total_definitions=total_defs,
    )


async def _eval_trend(session: AsyncSession, org_id: uuid.UUID) -> list[TrendBucket]:
    trend_q = (
        select(
            cast(EvalResult.evaluated_at, Date).label("bucket"),
            func.count().label("total"),
            func.sum(case((EvalResult.passed, 1), else_=0)).label("passed"),
            func.sum(case((EvalResult.passed.is_(False), 1), else_=0)).label("failed"),
        )
        .where(
            EvalResult.organisation_id == org_id,
            non_guardrail_eval_results_clause(),
        )
        .group_by(
            cast(EvalResult.evaluated_at, Date),
        )
        .order_by(
            cast(EvalResult.evaluated_at, Date),
        )
    )
    trend_rows = (await session.execute(trend_q)).all()

    return [
        TrendBucket(
            bucket=str(row.bucket),
            total=row.total,
            passed=row.passed,
            failed=row.failed,
        )
        for row in trend_rows
    ]


async def _eval_by_type(session: AsyncSession, org_id: uuid.UUID) -> list[TypeBreakdown]:
    by_type_q = (
        select(
            EvalDefinition.eval_type,
            func.count(EvalResult.id).label("total"),
            func.sum(case((EvalResult.passed, 1), else_=0)).label("passed"),
            func.sum(case((EvalResult.passed.is_(False), 1), else_=0)).label("failed"),
        )
        .outerjoin(EvalResult, EvalResult.eval_id == EvalDefinition.id)
        .where(
            EvalDefinition.organisation_id == org_id,
            EvalDefinition.eval_type != "guardrail",
        )
        .group_by(
            EvalDefinition.eval_type,
        )
        .order_by(
            EvalDefinition.eval_type,
        )
    )
    by_type_rows = (await session.execute(by_type_q)).all()

    return [
        TypeBreakdown(
            eval_type=row.eval_type,
            total=row.total,
            passed=row.passed,
            failed=row.failed,
        )
        for row in by_type_rows
    ]


async def _eval_coverage_gaps(session: AsyncSession, org_id: uuid.UUID) -> list[CoverageGap]:
    pipelines = (
        await session.execute(
            select(Pipeline.id, Pipeline.name, Pipeline.graph_nodes_json).where(Pipeline.organisation_id == org_id)
        )
    ).all()

    covered_pairs: set[tuple[uuid.UUID, str]] = set()
    eval_defs = (
        await session.execute(
            select(EvalDefinition.pipeline_id, EvalDefinition.node_id).where(EvalDefinition.organisation_id == org_id)
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
    return coverage_gaps


async def _eval_recent_results(session: AsyncSession, org_id: uuid.UUID) -> list[RecentEvalResult]:
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
          AND ed.eval_type != 'guardrail'
        ORDER BY er.evaluated_at DESC
        LIMIT 50
    """)
    recent_rows = (await session.execute(recent_q, {"org_id": org_id})).all()

    return [
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


@router.get("/evals/dashboard")
@handle_db_errors("admin.eval_dashboard")
async def eval_dashboard(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> EvalDashboardResponse:
    _require_admin(current_user, "access the eval dashboard")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            summary = await _eval_summary(session)
            trend = await _eval_trend(session, current_user.organisation_id)
            by_type = await _eval_by_type(session, current_user.organisation_id)
            coverage_gaps = await _eval_coverage_gaps(session, current_user.organisation_id)
            recent_results = await _eval_recent_results(session, current_user.organisation_id)
    except IntegrityError:
        logger.exception("admin.eval_dashboard")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.eval_dashboard")
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.warning("Eval dashboard DB error", exc_info=True)
        _raise_db_unavailable(_MSG_DATABASE_ERROR_PLEASE_TRY)

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
    recent_window_ratio: float
    lookback_days: int
    pipeline_id: str | None = None
    trend: str | None = None


@router.get("/evals/regressions")
@handle_db_errors("admin.eval_regressions")
async def eval_regressions(
    days: int = Query(default=7, ge=1, le=90, description="Lookback period in days"),
    threshold: float = Query(default=0.15, ge=0.0, le=1.0, description="Minimum drop fraction to trigger an alert"),
    recent_window_ratio: float = Query(
        default=0.25,
        gt=0.0,
        le=1.0,
        description="Fraction of the lookback period used as the recent window (e.g. 0.5 = last half)",
    ),
    pipeline_id: uuid.UUID | None = Query(
        default=None,
        description="Scope alerts to eval results from runs of a single pipeline",
    ),
    trend: str | None = Query(
        default=None,
        description=(
            "Filter alerts by trend direction: 'declining', 'stable' or 'improving'. "
            "Use 'declining' to surface true regressions only."
        ),
    ),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> RegressionAlertsResponse:
    _require_admin(current_user, "access eval regressions")

    if trend is not None and trend not in VALID_TRENDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"trend must be one of {sorted(VALID_TRENDS)}, got {trend!r}",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            alerts = await detect_regressions(
                session,
                org_id=current_user.organisation_id,
                days=days,
                threshold=threshold,
                recent_window_ratio=recent_window_ratio,
                pipeline_id=pipeline_id,
                trend=trend,
            )
    except IntegrityError:
        logger.exception("admin.eval_regressions")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.eval_regressions")
        logger.warning("Eval regressions unavailable — DB may need migration")
        _raise_feature_not_available()
    except TimeoutError:
        logger.exception("Eval regressions query timed out")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Query timed out. Please try again or reduce the lookback period.",
        ) from None
    except SQLAlchemyError:
        logger.exception("Eval regressions DB error")
        _raise_db_unavailable(_MSG_DATABASE_ERROR_PLEASE_TRY)
    except Exception:
        logger.exception("Eval regressions unexpected error")
        _raise_unexpected("An unexpected error occurred while checking eval regressions.")

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
        recent_window_ratio=recent_window_ratio,
        lookback_days=days,
        pipeline_id=str(pipeline_id) if pipeline_id is not None else None,
        trend=trend,
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


@router.get("/evals/okr-progress/{suite_id}")
@handle_db_errors("admin.okr_progress")
async def okr_progress(
    suite_id: str,
    target_date: str | None = Query(
        default=None,
        description="Optional ISO 8601 target date (e.g. 2026-09-30) for days-to-target",
    ),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> OkrProgressResponse:
    _require_admin(current_user, "access OKR progress")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            progress = await track_okr_progress(
                session,
                org_id=current_user.organisation_id,
                suite_id=suite_id,
                target_date=target_date,
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except IntegrityError:
        logger.exception("admin.okr_progress")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.okr_progress")
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception("OKR progress DB error")
        _raise_db_unavailable(_MSG_DATABASE_ERROR_PLEASE_TRY)
    except Exception:
        logger.exception("Unexpected error in OKR progress endpoint")
        _raise_unexpected("An unexpected error occurred. Please try again later.")

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
    trust_tier: str = Field(default="amber", pattern=_RE_GREEN_OR_AMBER)
    website_url: str | None = Field(None, max_length=2000)


class PublisherUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    contact_email: str | None = Field(None, max_length=255)
    public_key_hex: str | None = Field(None, min_length=64, max_length=128)
    trust_tier: str | None = Field(None, pattern=_RE_GREEN_OR_AMBER)
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


@router.get("/publishers")
@handle_db_errors("admin.admin_list_publishers")
async def admin_list_publishers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    trust_tier: str | None = Query(None, pattern=_RE_GREEN_OR_AMBER),
    search: str | None = Query(None, min_length=1),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> PublisherListResponse:
    _require_admin(current_user, "list publishers")

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
    except IntegrityError:
        logger.exception("admin.admin_list_publishers")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.admin_list_publishers")
        _raise_feature_not_available()

    return PublisherListResponse(
        items=[_to_publisher_response(p) for p in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("/publishers", status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.admin_create_publisher")
async def admin_create_publisher(
    req: PublisherCreateRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> PublisherResponse:
    _require_admin(current_user, "create publishers")

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
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc
    except IntegrityError:
        logger.exception("admin.admin_create_publisher")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.admin_create_publisher")
        _raise_feature_not_available()

    return _to_publisher_response(publisher)


async def _enforce_publisher_name_uniqueness(
    session: AsyncSession,
    org_id: uuid.UUID,
    name_val: object,
    publisher_id: uuid.UUID,
) -> None:
    if not isinstance(name_val, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="publisher_name_invalid: Name must be a string",
        )
    existing = await get_publisher_by_name(session, org_id, name_val)
    if existing is not None and existing.id != publisher_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A publisher with this name already exists",
        )


async def _enforce_publisher_key_uniqueness(
    session: AsyncSession,
    org_id: uuid.UUID,
    key_val: object,
    publisher_id: uuid.UUID,
) -> None:
    if not isinstance(key_val, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="publisher_key_invalid: Public key must be a string",
        )
    existing_key = await get_publisher_by_key(session, org_id, key_val)
    if existing_key is not None and existing_key.id != publisher_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A publisher with this public key already exists",
        )


@router.put("/publishers/{publisher_id}")
@handle_db_errors("admin.admin_update_publisher")
async def admin_update_publisher(
    publisher_id: uuid.UUID,
    req: PublisherUpdateRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> PublisherResponse:
    _require_admin(current_user, "update publishers")

    updates: dict[str, object] = req.model_dump(exclude_unset=True)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            if "name" in updates:
                await _enforce_publisher_name_uniqueness(
                    session,
                    current_user.organisation_id,
                    updates["name"],
                    publisher_id,
                )

            if "public_key_hex" in updates:
                await _enforce_publisher_key_uniqueness(
                    session,
                    current_user.organisation_id,
                    updates["public_key_hex"],
                    publisher_id,
                )

            try:
                publisher = await crud_update_publisher(
                    session, publisher_id, updates, org_id=current_user.organisation_id
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc
    except IntegrityError:
        logger.exception("admin.admin_update_publisher")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.admin_update_publisher")
        _raise_feature_not_available()

    if publisher is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Publisher not found",
        )

    return _to_publisher_response(publisher)


@router.delete("/publishers/{publisher_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("admin.admin_delete_publisher")
async def admin_delete_publisher(
    publisher_id: uuid.UUID,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user, "delete publishers")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            deleted = await crud_delete_publisher(session, publisher_id, org_id=current_user.organisation_id)
    except IntegrityError:
        logger.exception("admin.admin_delete_publisher")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.admin_delete_publisher")
        _raise_feature_not_available()

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Publisher not found",
        )


# ── Run Retention / Purge ──────────────────────────────────────────────


class RetentionPurgeRequest(BaseModel):
    max_age_days: int = 90


@router.post("/purge/runs", status_code=status.HTTP_200_OK, dependencies=[require_feature("admin_run_retention")])
async def admin_retention_purge_runs(
    req: RetentionPurgeRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    _require_admin(current_user, "trigger run retention purge")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            deleted = await batch_delete_old_terminal_runs(session, max_age_days=req.max_age_days)

    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin.admin_retention_purge_runs")
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_this_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_db_error_occurred()
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_unexpected(MSG_UNEXPECTED_ERROR)

    return {"deleted_run_count": deleted}


class ManualPurgeRequest(BaseModel):
    older_than: str


@router.post("/purge", status_code=status.HTTP_200_OK, dependencies=[require_feature("admin_run_retention")])
async def admin_manual_purge(
    req: ManualPurgeRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    _require_admin(current_user, "purge runs")

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
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin.admin_manual_purge")
        _raise_conflict()
    except ProgrammingError:
        logger.exception("admin.admin_manual_purge")
        _raise_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_db_error_occurred()
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_unexpected(MSG_UNEXPECTED_ERROR)

    return result


class PurgeRunsRequest(BaseModel):
    older_than_days: int = 90


class PurgeRunsResponse(BaseModel):
    purged_count: int


@router.post("/runs/purge", status_code=status.HTTP_200_OK, dependencies=[require_feature("admin_run_retention")])
async def admin_purge_stale_runs(
    request: PurgeRunsRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> PurgeRunsResponse:
    _require_admin(current_user, "purge stale runs")

    cutoff = datetime.now(UTC) - timedelta(days=request.older_than_days)
    terminal_states = TERMINAL_STATUSES

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            result = await session.execute(
                delete(Run).where(
                    Run.organisation_id == current_user.organisation_id,
                    Run.status.in_(terminal_states),
                    Run.created_at < cutoff,
                )
            )

    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin.admin_purge_stale_runs")
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_this_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_db_error_occurred()
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_unexpected(MSG_UNEXPECTED_ERROR)

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


@router.get(
    "/runs/retention",
    dependencies=[require_feature("admin_run_retention")],
)
async def admin_get_retention(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> RetentionConfigResponse:
    _require_admin(current_user, "view retention")
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            result = await session.execute(
                select(Organisation.settings_json).where(Organisation.id == current_user.organisation_id).limit(1)
            )
            row = result.scalar_one_or_none()
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin.admin_get_retention")
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_this_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_db_error_occurred()
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_unexpected(MSG_UNEXPECTED_ERROR)

    retention_days = 90
    if isinstance(row, dict):
        raw = row.get("retention_days", 90)
        if isinstance(raw, bool):
            retention_days = 90
        elif isinstance(raw, int) and raw > 0:
            retention_days = raw
        elif isinstance(raw, str) and raw.isdigit():
            retention_days = int(raw)
    return RetentionConfigResponse(retention_days=retention_days)


@router.put("/runs/retention", status_code=status.HTTP_200_OK, dependencies=[require_feature("admin_run_retention")])
async def admin_update_retention(
    req: UpdateRetentionRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> RetentionConfigResponse:
    _require_admin(current_user, "update retention")
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            result = await session.execute(
                select(Organisation).where(Organisation.id == current_user.organisation_id).limit(1)
            )
            org = result.scalar_one_or_none()
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)
            _update_org_setting(org, "retention_days", req.retention_days)
            await session.flush()
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin.admin_update_retention")
        _raise_conflict()
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_this_feature_not_available()
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_db_error_occurred()
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        _raise_unexpected(MSG_UNEXPECTED_ERROR)

    logger.info(
        "run_retention.updated",
        extra={
            "org_id": str(current_user.organisation_id),
            "retention_days": req.retention_days,
        },
    )
    return RetentionConfigResponse(retention_days=req.retention_days)


# ── Org Sandbox Concurrency Limit ─────────────────────────────────────────
# Org self-service route: principal's own org only (never from path/body), so
# cross-org writes are structurally impossible.


class SandboxConcurrencyResponse(BaseModel):
    sandbox_concurrency_limit: int | None = None


class UpdateSandboxConcurrencyRequest(BaseModel):
    sandbox_concurrency_limit: int | None = Field(default=None, ge=1, le=100)


@router.get("/org/sandbox-concurrency")
async def admin_get_sandbox_concurrency(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> SandboxConcurrencyResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can view sandbox concurrency",
        )
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            limit = await get_sandbox_concurrency_limit(session, current_user.organisation_id)
    except asyncio.CancelledError:
        raise
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SandboxConcurrencyResponse(sandbox_concurrency_limit=limit)


@router.put("/org/sandbox-concurrency", status_code=status.HTTP_200_OK)
async def admin_update_sandbox_concurrency(
    req: UpdateSandboxConcurrencyRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> SandboxConcurrencyResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update sandbox concurrency",
        )
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)
            settings = dict(org.settings_json) if org.settings_json else {}
            settings["sandbox_concurrency_limit"] = req.sandbox_concurrency_limit
            org.settings_json = settings
            await session.flush()
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin.admin_update_sandbox_concurrency")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="org.sandbox_concurrency_updated",
                actor_user_id=current_user.account_id,
                resource_type="organisation",
                resource_id=current_user.organisation_id,
                payload_json={"sandbox_concurrency_limit": req.sandbox_concurrency_limit},
            )
    except IntegrityError:
        logger.exception("admin.admin_update_sandbox_concurrency.audit")
    except ProgrammingError:
        logger.warning(
            "sandbox_concurrency audit event ProgrammingError — limit was updated",
            extra={
                "org_id": str(current_user.organisation_id),
                "sandbox_concurrency_limit": req.sandbox_concurrency_limit,
            },
        )
    except SQLAlchemyError:
        logger.warning(
            "sandbox_concurrency audit event SQLAlchemyError — limit was updated",
            extra={
                "org_id": str(current_user.organisation_id),
                "sandbox_concurrency_limit": req.sandbox_concurrency_limit,
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "admin.admin_update_sandbox_concurrency.audit",
            extra={
                "org_id": str(current_user.organisation_id),
                "sandbox_concurrency_limit": req.sandbox_concurrency_limit,
            },
        )

    logger.info(
        "sandbox_concurrency.updated",
        extra={
            "org_id": str(current_user.organisation_id),
            "sandbox_concurrency_limit": req.sandbox_concurrency_limit,
        },
    )
    return SandboxConcurrencyResponse(sandbox_concurrency_limit=req.sandbox_concurrency_limit)


# ── Org Run Concurrency Limit ──────────────────────────────────────────────
# Org self-service route: principal's own org only (never from path/body), so
# cross-org writes are structurally impossible. Mirrors the sandbox-concurrency
# endpoints above, but gates org-wide RUN concurrency (all pipeline runs) rather
# than sandbox-agent runs. The two caps are independent org settings and share
# the ``org_capacity_limited`` error-code marker on deferred runs.


class RunConcurrencyResponse(BaseModel):
    run_concurrency_limit: int | None = None


class UpdateRunConcurrencyRequest(BaseModel):
    run_concurrency_limit: int | None = Field(default=None, ge=1, le=100)


@router.get("/org/run-concurrency")
async def admin_get_run_concurrency(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> RunConcurrencyResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can view run concurrency",
        )
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            limit = await get_org_run_concurrency_limit(session, current_user.organisation_id)
    except asyncio.CancelledError:
        raise
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return RunConcurrencyResponse(run_concurrency_limit=limit)


@router.put("/org/run-concurrency", status_code=status.HTTP_200_OK)
async def admin_update_run_concurrency(
    req: UpdateRunConcurrencyRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> RunConcurrencyResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can update run concurrency",
        )
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)
            settings = dict(org.settings_json) if org.settings_json else {}
            settings["run_concurrency_limit"] = req.run_concurrency_limit
            org.settings_json = settings
            await session.flush()
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin.admin_update_run_concurrency")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="org.run_concurrency_updated",
                actor_user_id=current_user.account_id,
                resource_type="organisation",
                resource_id=current_user.organisation_id,
                payload_json={"run_concurrency_limit": req.run_concurrency_limit},
            )
    except IntegrityError:
        logger.exception("admin.admin_update_run_concurrency.audit")
    except ProgrammingError:
        logger.warning(
            "run_concurrency audit event ProgrammingError — limit was updated",
            extra={
                "org_id": str(current_user.organisation_id),
                "run_concurrency_limit": req.run_concurrency_limit,
            },
        )
    except SQLAlchemyError:
        logger.warning(
            "run_concurrency audit event SQLAlchemyError — limit was updated",
            extra={
                "org_id": str(current_user.organisation_id),
                "run_concurrency_limit": req.run_concurrency_limit,
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "admin.admin_update_run_concurrency.audit",
            extra={
                "org_id": str(current_user.organisation_id),
                "run_concurrency_limit": req.run_concurrency_limit,
            },
        )

    logger.info(
        "run_concurrency.updated",
        extra={
            "org_id": str(current_user.organisation_id),
            "run_concurrency_limit": req.run_concurrency_limit,
        },
    )
    return RunConcurrencyResponse(run_concurrency_limit=req.run_concurrency_limit)


@router.get("/runs/storage")
@handle_db_errors("admin.admin_get_storage")
async def admin_get_storage(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> StorageInfoResponse:
    if current_user.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin users can view storage")
    try:
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

    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    breakdown: dict[str, int] = {row.status: row.cnt for row in status_rows}

    terminal_states = TERMINAL_STATUSES
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


@router.get("/hitl/overdue")
@handle_db_errors("admin.admin_overdue_hitl_claims")
async def admin_overdue_hitl_claims(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> OverdueClaimsResponse:
    """List overdue HITL claims across the organisation."""
    if current_user.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            claims = await get_overdue_claims(session, current_user.organisation_id)

    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ADMIN)

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return OverdueClaimsResponse(claims=[OverdueClaimItem(**c) for c in claims])
