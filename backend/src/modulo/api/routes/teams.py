"""Team management REST routes."""

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.team_rbac import (
    org_role_level,
    team_role_level,
)
from modulo.db.crud import account as _account_crud
from modulo.db.crud.org_membership import get_membership_by_account_and_org
from modulo.db.crud.team import (
    TeamUpdateOutcome,
    create_team,
    delete_team,
    get_team,
    get_team_by_name,
    list_teams,
    reassign_team_resources_to_org,
    update_team,
    update_team_if_unchanged,
)
from modulo.db.crud.team_membership import (
    add_team_member,
    get_membership,
    get_membership_by_team_and_account,
    list_team_members,
    list_team_memberships_for_account,
    remove_team_member,
    update_member_role,
)
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_TEAM_LIST = "team.list"
_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE = "Database temporarily unavailable. Please try again."
_MSG_TEAM_NAME_ALREADY_EXISTS = "A team with this name already exists in your organisation"
_MSG_TEAM_NOT_FOUND = "Team not found"
_MSG_MEMBERSHIP_NOT_FOUND = "Membership not found"


_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/teams",
    tags=["teams"],
    dependencies=[require_feature("team_rbac")],
)


async def _assert_not_last_operator(
    session: AsyncSession,
    team_id: uuid.UUID,
    except_membership_id: uuid.UUID,
) -> None:
    """Block removing/demoting the last ``operator``-role member of a team.

    Mirrors the org-level ``assert_not_last_admin`` guard: a team with members
    must retain at least one operator so it stays self-manageable. Removing or
    demoting the only operator while other members remain would strand the
    team without anyone able to manage membership. Emptying the team entirely
    (removing the sole member) is allowed.
    """
    other_members = (
        await session.execute(
            select(func.count())
            .select_from(TeamMembership)
            .where(
                TeamMembership.team_id == team_id,
                TeamMembership.id != except_membership_id,
            )
        )
    ).scalar_one() or 0
    if other_members == 0:
        return

    other_operators = (
        await session.execute(
            select(func.count())
            .select_from(TeamMembership)
            .where(
                TeamMembership.team_id == team_id,
                TeamMembership.id != except_membership_id,
                TeamMembership.role == "operator",
            )
        )
    ).scalar_one() or 0
    if other_operators == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the last operator from the team. Promote another member to operator first.",
        )


async def _require_team_operator_caller(
    session: AsyncSession,
    team_id: uuid.UUID,
    account_id: uuid.UUID,
    detail: str,
) -> TeamMembership:
    """Return the caller's team membership, requiring it to be an ``operator``."""
    membership = await get_membership_by_team_and_account(session, team_id, account_id)
    if membership is None or membership.role != "operator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return membership


async def _load_team_membership(session: AsyncSession, team_id: uuid.UUID, membership_id: uuid.UUID) -> TeamMembership:
    """Load a membership scoped to ``team_id``, 404 on mismatch/absence."""
    membership = await get_membership(session, membership_id)
    if membership is None or membership.team_id != team_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MEMBERSHIP_NOT_FOUND)
    return membership


async def _apply_team_update(
    session: AsyncSession,
    org_id: uuid.UUID,
    team_id: uuid.UUID,
    updates: dict[str, Any],
    expected_updated_at: str | None,
) -> Team:
    """Apply name/description/optimistic-version updates, raising 409/404 on conflicts."""
    if "name" in updates:
        existing = await get_team_by_name(session, org_id, updates["name"])
        if existing is not None and existing.id != team_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_TEAM_NAME_ALREADY_EXISTS)

    if expected_updated_at is not None:
        outcome, team = await update_team_if_unchanged(session, team_id, updates, expected_updated_at)
        if outcome is TeamUpdateOutcome.NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)
        if outcome is TeamUpdateOutcome.STALE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("Team was modified by another request. Refresh and try again (optimistic lock mismatch)."),
            )
        if team is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)
        return team
    team = await update_team(session, team_id, updates)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)
    return team


async def _add_team_member_checked(
    session: AsyncSession,
    current_user: TenantPrincipal,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> TeamMembership:
    """Authorise and add a team member, enforcing the org/team role ceilings."""
    is_admin = org_role_level(current_user.org_role) >= org_role_level("admin")
    if not is_admin:
        caller_membership = await _require_team_operator_caller(
            session,
            team_id,
            current_user.account_id,
            "Only admin users or team operators can add members",
        )
        if team_role_level(role) > team_role_level(caller_membership.role):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Cannot grant role '{role}' above your own team role '{caller_membership.role}'",
            )

    target_account = await _account_crud.get_account_by_id(session, user_id)
    target_membership = await get_membership_by_account_and_org(session, user_id, current_user.organisation_id)
    if target_account is None or target_membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organisation")
    if team_role_level(role) > org_role_level(target_membership.role):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Team role '{role}' exceeds user's org role '{target_membership.role}'",
        )
    return await add_team_member(
        session,
        org_id=current_user.organisation_id,
        team_id=team_id,
        account_id=user_id,
        role=role,
    )


async def _remove_member_checked(
    session: AsyncSession,
    current_user: TenantPrincipal,
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
) -> TeamMembership:
    """Authorise and remove a team member, enforcing the last-operator guard."""
    is_admin = org_role_level(current_user.org_role) >= org_role_level("admin")
    caller_membership = None
    if not is_admin:
        caller_membership = await _require_team_operator_caller(
            session,
            team_id,
            current_user.account_id,
            "Only admin users or team operators can remove members",
        )
    membership = await _load_team_membership(session, team_id, membership_id)

    # SECURITY (#1194): operator cannot remove someone with equal or higher
    # team role — prevents intra-org privilege interference.
    if not is_admin:
        assert caller_membership is not None  # nosec B101 -- genuine invariant: when not is_admin, caller_membership is set to the operator membership above
        target_level = team_role_level(membership.role)
        caller_level = team_role_level(caller_membership.role)
        if target_level >= caller_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Cannot remove member with role '{membership.role}' — your role is '{caller_membership.role}'"
                ),
            )

    if membership.role == "operator":
        await _assert_not_last_operator(session, team_id, membership_id)
    await remove_team_member(session, membership_id)
    return membership


async def _change_member_role_checked(
    session: AsyncSession,
    current_user: TenantPrincipal,
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
    new_role: str,
) -> tuple[TeamMembership, str]:
    """Authorise and apply a member role change; returns (membership, old_role)."""
    is_admin = org_role_level(current_user.org_role) >= org_role_level("admin")
    caller_membership = None
    if not is_admin:
        caller_membership = await _require_team_operator_caller(
            session,
            team_id,
            current_user.account_id,
            "Only admin users or team operators can change member roles",
        )
        if team_role_level(new_role) > team_role_level(caller_membership.role):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Cannot grant role '{new_role}' above your own team role '{caller_membership.role}'",
            )

    existing = await _load_team_membership(session, team_id, membership_id)
    old_role = existing.role

    # SECURITY (#1194): operator cannot demote someone with equal or higher
    # team role — prevents intra-org privilege interference.
    if not is_admin:
        assert caller_membership is not None  # nosec B101 -- genuine invariant: when not is_admin, caller_membership is set to the operator membership above
        target_level = team_role_level(old_role)
        caller_level = team_role_level(caller_membership.role)
        if target_level >= caller_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Cannot change role of member with role '{old_role}' — your role is '{caller_membership.role}'"
                ),
            )

    if old_role == "operator" and new_role != "operator":
        await _assert_not_last_operator(session, team_id, membership_id)
    membership = await update_member_role(session, membership_id, new_role)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MEMBERSHIP_NOT_FOUND)
    return membership, old_role


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
    expected_updated_at: str | None = None

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


class TeamReassignResponse(BaseModel):
    team_id: str
    reassigned: int


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


class MyTeamResponse(BaseModel):
    team_id: str
    team_name: str
    role: str


@router.get("/my")
@handle_db_errors("teams.my_teams_endpoint")
async def my_teams_endpoint(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[MyTeamResponse]:
    """List the current user's team memberships with team names (profile "My Teams")."""
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            memberships = await list_team_memberships_for_account(session, current_user.account_id)
            team_ids = [m.team_id for m in memberships]
            names: dict[uuid.UUID, str] = {}
            if team_ids:
                rows = (
                    await session.execute(
                        select(Team.id, Team.name).where(
                            Team.id.in_(team_ids),
                            Team.deleted_at.is_(None),
                        )
                    )
                ).all()
                names = {row[0]: row[1] for row in rows}
    except ProgrammingError:
        _log.exception("teams.my_teams_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("my_teams SQLAlchemyError", extra={"org_id": str(current_user.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("my_teams unexpected error", extra={"org_id": str(current_user.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching your teams.",
        ) from None

    return [
        MyTeamResponse(
            team_id=str(m.team_id),
            team_name=names.get(m.team_id, ""),
            role=m.role,
        )
        for m in memberships
        if m.team_id in names
    ]


@router.get("")
@handle_db_errors("teams.list_teams_endpoint")
async def list_teams_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: TenantPrincipal = require_permission(_CODE_TEAM_LIST),
    session: AsyncSession = Depends(get_db_session),
) -> TeamListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            result = await list_teams(session, org_id=current_user.organisation_id, page=page, page_size=page_size)
    except IntegrityError as exc:
        _log.exception("teams.list_teams_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.list_teams_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("list_teams SQLAlchemyError", extra={"org_id": str(current_user.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except Exception:
        _log.exception("list_teams unexpected error", extra={"org_id": str(current_user.organisation_id)})
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
                created_at=t.created_at.isoformat() if t.created_at else "",
            )
            for t in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("teams.create_team_endpoint")
async def create_team_endpoint(
    req: CreateTeamRequest,
    current_user: TenantPrincipal = require_permission("team.create"),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:

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
    except IntegrityError:
        _log.exception("teams.create_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_TEAM_NAME_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("teams.create_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("create_team SQLAlchemyError", extra={"org_id": str(current_user.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("create_team unexpected error", extra={"org_id": str(current_user.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the team.",
        ) from None

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="team_created",
                actor_user_id=current_user.account_id,
                resource_type="team",
                resource_id=team.id,
                payload_json={"team_id": str(team.id), "name": team.name},
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "create_team audit event failed — team was created",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team.id)},
        )

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat() if team.created_at else "",
    )


@router.get("/{team_id}")
@handle_db_errors("teams.get_team_endpoint")
async def get_team_endpoint(
    team_id: uuid.UUID,
    current_user: TenantPrincipal = require_permission(_CODE_TEAM_LIST),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            team = await get_team(session, team_id)
    except IntegrityError as exc:
        _log.exception("teams.get_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.get_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "get_team SQLAlchemyError", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "get_team unexpected error", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching the team.",
        ) from None

    if team is None or team.organisation_id != current_user.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat() if team.created_at else "",
    )


@router.patch("/{team_id}")
@handle_db_errors("teams.update_team_endpoint")
async def update_team_endpoint(
    team_id: uuid.UUID,
    req: UpdateTeamRequest,
    current_user: TenantPrincipal = require_permission("team.update"),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:

    updates = req.model_dump(exclude_unset=True)
    updates.pop("expected_updated_at", None)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            team = await _apply_team_update(
                session,
                current_user.organisation_id,
                team_id,
                updates,
                req.expected_updated_at,
            )
    except IntegrityError as exc:
        _log.exception("teams.update_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.update_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "update_team SQLAlchemyError", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "update_team unexpected error", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the team.",
        ) from None

    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="team_updated",
                actor_user_id=current_user.account_id,
                resource_type="team",
                resource_id=team_id,
                payload_json={"team_id": str(team_id), "updates": updates},
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "update_team audit event failed — team was updated",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )

    return TeamResponse(
        id=str(team.id),
        name=team.name,
        description=team.description,
        account_id=str(team.account_id),
        created_at=team.created_at.isoformat() if team.created_at else "",
    )


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("teams.delete_team_endpoint")
async def delete_team_endpoint(
    team_id: uuid.UUID,
    current_user: TenantPrincipal = require_permission("team.delete"),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            from sqlalchemy import func, select

            from modulo.db.models.connector_instance import ConnectorInstance
            from modulo.db.models.library_primitive import LibraryPrimitive
            from modulo.db.models.model_backend import ModelBackend
            from modulo.db.models.pipeline import Pipeline

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
    except IntegrityError as exc:
        _log.exception("teams.delete_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.delete_team_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "delete_team SQLAlchemyError", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "delete_team unexpected error", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting the team.",
        ) from None

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

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
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "delete_team audit event failed — team was deleted",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )


@router.post("/{team_id}/reassign-org")
@handle_db_errors("teams.reassign_team_resources_endpoint")
async def reassign_team_resources_endpoint(
    team_id: uuid.UUID,
    current_user: TenantPrincipal = require_permission("team.delete"),
    session: AsyncSession = Depends(get_db_session),
) -> TeamReassignResponse:
    """Reassign every team-owned resource to org-wide (PRD §9.3 Team Deletion Policy).

    Sets ``owner_team_id = NULL`` (and flips ``visibility`` to ``'org'``, keeping
    the ``ck_*_team_owner`` CHECK constraints satisfied) on every pipeline,
    connector instance, model backend and library primitive currently owned by
    the team, so the team can then be deleted (deletion is blocked while
    ``owner_team_id`` references the team). Admin-only (``team.delete``).
    Idempotent: re-running after a successful reassignment finds zero owned
    rows and returns ``reassigned=0``.
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            team = await get_team(session, team_id)
            if team is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

            total, _touched = await reassign_team_resources_to_org(
                session,
                org_id=current_user.organisation_id,
                team_id=team_id,
            )
    except IntegrityError as exc:
        _log.exception("teams.reassign_team_resources_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from exc
    except ProgrammingError:
        _log.exception("teams.reassign_team_resources_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "reassign_team_resources SQLAlchemyError",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "reassign_team_resources unexpected error",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while reassigning the team's resources.",
        ) from None

    return TeamReassignResponse(team_id=str(team_id), reassigned=total)


@router.get("/{team_id}/members")
@handle_db_errors("teams.list_members_endpoint")
async def list_members_endpoint(
    team_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: TenantPrincipal = require_permission(_CODE_TEAM_LIST),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            team = await get_team(session, team_id)
            if team is None or team.organisation_id != current_user.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)
            result = await list_team_members(session, team_id=team_id, page=page, page_size=page_size)
    except IntegrityError as exc:
        _log.exception("teams.list_members_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.list_members_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "list_members SQLAlchemyError", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "list_members unexpected error",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing members.",
        ) from None

    return MembershipListResponse(
        items=[
            MembershipResponse(
                id=str(m.id),
                team_id=str(m.team_id),
                user_id=str(m.account_id),
                role=m.role,
                created_at=m.created_at.isoformat() if m.created_at else "",
            )
            for m in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/{team_id}/members",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("teams.add_member_endpoint")
async def add_member_endpoint(
    team_id: uuid.UUID,
    req: AddMemberRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipResponse:
    user_id = uuid.UUID(req.user_id)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            team = await get_team(session, team_id)
            if team is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

            membership = await _add_team_member_checked(session, current_user, team_id, user_id, req.role)
    except IntegrityError as exc:
        _log.exception("teams.add_member_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.add_member_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "add_member SQLAlchemyError", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "add_member unexpected error", extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while adding the member.",
        ) from None

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="team_member_added",
                actor_user_id=current_user.account_id,
                resource_type="team_membership",
                resource_id=membership.id,
                payload_json={
                    "team_id": str(team_id),
                    "user_id": str(membership.account_id),
                    "role": membership.role,
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "add_member audit event failed — member was added",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )

    return MembershipResponse(
        id=str(membership.id),
        team_id=str(membership.team_id),
        user_id=str(membership.account_id),
        role=membership.role,
        created_at=membership.created_at.isoformat() if membership.created_at else "",
    )


@router.delete(
    "/{team_id}/members/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@handle_db_errors("teams.remove_member_endpoint")
async def remove_member_endpoint(
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            membership = await _remove_member_checked(session, current_user, team_id, membership_id)
    except IntegrityError as exc:
        _log.exception("teams.remove_member_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.remove_member_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "remove_member SQLAlchemyError",
            extra={
                "org_id": str(current_user.organisation_id),
                "team_id": str(team_id),
                "membership_id": str(membership_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "remove_member unexpected error",
            extra={
                "org_id": str(current_user.organisation_id),
                "team_id": str(team_id),
                "membership_id": str(membership_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while removing the member.",
        ) from None

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="team_member_removed",
                actor_user_id=current_user.account_id,
                resource_type="team_membership",
                resource_id=membership_id,
                payload_json={
                    "team_id": str(team_id),
                    "user_id": str(membership.account_id),
                    "role": membership.role,
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "remove_member audit event failed — member was removed",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )


@router.patch(
    "/{team_id}/members/{membership_id}",
)
@handle_db_errors("teams.change_member_role_endpoint")
async def change_member_role_endpoint(
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
    req: ChangeMemberRoleRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            team = await get_team(session, team_id)
            if team is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TEAM_NOT_FOUND)

            membership, old_role = await _change_member_role_checked(
                session,
                current_user,
                team_id,
                membership_id,
                req.role,
            )
    except IntegrityError as exc:
        _log.exception("teams.change_member_role_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from exc
    except ProgrammingError:
        _log.exception("teams.change_member_role_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(
            "change_member_role SQLAlchemyError",
            extra={
                "org_id": str(current_user.organisation_id),
                "team_id": str(team_id),
                "membership_id": str(membership_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(
            "change_member_role unexpected error",
            extra={
                "org_id": str(current_user.organisation_id),
                "team_id": str(team_id),
                "membership_id": str(membership_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while changing the member role.",
        ) from None

    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="team_member_role_changed",
                actor_user_id=current_user.account_id,
                resource_type="team_membership",
                resource_id=membership.id,
                payload_json={
                    "team_id": str(team_id),
                    "user_id": str(membership.account_id),
                    "old_role": old_role,
                    "new_role": membership.role,
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "change_member_role audit event failed — member role was changed",
            extra={"org_id": str(current_user.organisation_id), "team_id": str(team_id)},
        )

    return MembershipResponse(
        id=str(membership.id),
        team_id=str(membership.team_id),
        user_id=str(membership.account_id),
        role=membership.role,
        created_at=membership.created_at.isoformat() if membership.created_at else "",
    )
