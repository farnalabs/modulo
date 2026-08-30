"""ViewModel aggregate API.

GET /api/v1/me       — current user info (canonical; auth/me also works)
GET /api/v1/viewmodel/current — single-request aggregate for the frontend
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import resolve_plan_context
from modulo.db.crud.account import get_account_by_id
from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.pipeline import list_pipelines
from modulo.db.crud.run import list_runs
from modulo.db.crud.team_membership import list_team_memberships_for_account
from modulo.db.crud.view import get_view, list_views
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.team import Team
from modulo.db.models.view import SavedView
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Keys from settings_json that are safe to expose in the API response.
# Secret fields (license_key, smtp_password, api keys) are excluded.
_SAFE_ORG_SETTINGS_KEYS = frozenset(
    {
        "logo_url",
        "feature_overrides",
        "timezone",
        "locale",
    }
)

router = APIRouter(tags=["viewmodel"])

# ---------------------------------------------------------------------------
# Shared sub-schemas
# ---------------------------------------------------------------------------


class UserInfo(BaseModel):
    username: str


class OrganisationInfo(BaseModel):
    org_id: uuid.UUID
    org_name: str
    settings: dict[str, object]


class TeamMembershipInfo(BaseModel):
    team_id: uuid.UUID
    team_role: str


class MeResponse(BaseModel):
    user: UserInfo
    org: OrganisationInfo
    team_memberships: list[TeamMembershipInfo]
    team_memberships_truncated: bool
    org_role: str
    preferences: dict[str, Any] = Field(default_factory=dict)
    is_system_admin: bool = False
    must_change_password: bool = False


class PipelineSummary(BaseModel):
    id: uuid.UUID
    name: str
    visibility: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RunSummary(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    status: str
    trigger_type: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PendingHitlGate(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    pipeline_id: uuid.UUID
    gate_id: str
    claimed_by: uuid.UUID | None
    expires_at: datetime | None
    required_team_id: uuid.UUID | None = None
    required_team_name: str | None = None

    model_config = {"from_attributes": True}


class LicenseInfo(BaseModel):
    tier: str = "community"
    features: list[str] = Field(default_factory=list)
    is_valid: bool = True


class ViewInfo(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    view_type: str
    filters: dict[str, Any]
    columns: list[str] | None
    sort_by: str | None
    sort_order: str
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_by_me: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ViewModelViewsResponse(BaseModel):
    items: list[ViewInfo]
    total: int
    page: int
    page_size: int
    run_list_views: list[ViewInfo]
    pipeline_list_views: list[ViewInfo]
    audit_log_views: list[ViewInfo]


class FeatureFlagInfo(BaseModel):
    name: str
    description: str
    tier: str
    active: bool


class PlanInfo(BaseModel):
    tier: str
    daily_spend_limit: float | None = None


class ViewModelCurrent(BaseModel):
    user: UserInfo
    org: OrganisationInfo
    org_role: str
    team_memberships: list[TeamMembershipInfo]
    team_memberships_truncated: bool
    preferences: dict[str, Any]
    feature_flags: list[FeatureFlagInfo]
    plan: PlanInfo
    pipelines: list[PipelineSummary]
    pipelines_total: int
    recent_runs: list[RunSummary]
    runs_total: int
    pending_hitl_gates: list[PendingHitlGate]
    views: list[ViewInfo] | None = None
    current_view: ViewInfo | None = None
    is_system_admin: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/v1/license")
@handle_db_errors("viewmodel.license_info")
async def license_info(
    settings: Settings = Depends(get_settings),
) -> LicenseInfo:
    try:
        has_license_key = bool(settings.modulo_license_key)
        features: list[str] = []
        if has_license_key:
            features = ["notifications"]
        return LicenseInfo(
            tier="team" if has_license_key else "community",
            features=features,
            is_valid=True,
        )
    except Exception:
        logger.exception("license_info")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve license information",
        ) from None


@router.get("/api/v1/me")
@handle_db_errors("viewmodel.me")
async def me(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role or "admin")
            memberships = await list_team_memberships_for_account(session, current_user.account_id)
            account = await get_account_by_id(session, current_user.account_id)
    except ProgrammingError:
        logger.exception("viewmodel.me")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("me.failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while loading user info.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("me.failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load user info.",
        ) from None

    return MeResponse(
        user=UserInfo(username=current_user.username),
        org=OrganisationInfo(
            org_id=current_user.organisation_id,
            org_name="Modulo",
            settings={},
        ),
        team_memberships=[TeamMembershipInfo(team_id=m.team_id, team_role=m.role) for m in memberships],
        team_memberships_truncated=False,
        org_role=current_user.org_role,
        preferences=account.preferences if account is not None else {},
        is_system_admin=current_user.is_system_admin,
        must_change_password=bool(account.must_change_password) if account is not None else False,
    )


@router.get("/api/v1/viewmodel/current")
@handle_db_errors("viewmodel.viewmodel_current")
async def viewmodel_current(
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    view_as_team: uuid.UUID | None = Query(None),
    current_view_id: uuid.UUID | None = Query(None),
) -> ViewModelCurrent:
    if view_as_team is not None and current_user.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can use view_as_team")

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role or "")

            if view_as_team is not None and current_user.organisation_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot use view_as_team without an organisation",
                )

            if view_as_team is not None:
                team_result = await session.execute(
                    select(Team).where(
                        Team.id == view_as_team,
                        Team.organisation_id == current_user.organisation_id,
                        Team.deleted_at.is_(None),
                    )
                )
                team = team_result.scalar_one_or_none()
                if team is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

            org = None
            if current_user.organisation_id is not None:
                org = await get_organisation(session, current_user.organisation_id)
                if org is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            elif not current_user.is_system_admin:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

            account = await get_account_by_id(session, current_user.account_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

            memberships = await list_team_memberships_for_account(session, current_user.account_id)

            if current_user.organisation_id is not None:
                pipelines_page = await list_pipelines(session, page=1, page_size=20)
                runs_page = await list_runs(session, page=1, page_size=10)

                user_team_ids = [m.team_id for m in memberships]
                hitl_query = (
                    select(HitlClaim, Team.name.label("required_team_name"))
                    .outerjoin(Team, HitlClaim.required_team_id == Team.id)
                    .where(
                        HitlClaim.organisation_id == current_user.organisation_id,
                        HitlClaim.decision.is_(None),
                    )
                )
                if user_team_ids:
                    hitl_query = hitl_query.where(
                        HitlClaim.required_team_id.is_(None) | HitlClaim.required_team_id.in_(user_team_ids)
                    )
                else:
                    hitl_query = hitl_query.where(HitlClaim.required_team_id.is_(None))

                pending_hitl_result = await session.execute(hitl_query)
                pending_hitl = [
                    PendingHitlGate(
                        id=h.id,
                        run_id=h.run_id,
                        pipeline_id=h.pipeline_id,
                        gate_id=h.gate_id,
                        claimed_by=h.account_id,
                        expires_at=h.expires_at,
                        required_team_id=h.required_team_id,
                        required_team_name=team_name,
                    )
                    for h, team_name in pending_hitl_result.all()
                ]

                all_views_result = await list_views(session, page=1, page_size=100)
                all_views = [_enrich_view(v, current_user.account_id) for v in all_views_result.items]

                current_view = None
                if current_view_id is not None:
                    view = await get_view(session, current_view_id)
                    if view is not None:
                        current_view = _enrich_view(view, current_user.account_id)
            else:
                pipelines_page = None
                runs_page = None
                pending_hitl = []
                all_views = []
                current_view = None

            plan_ctx = await resolve_plan_context(settings, session, org=org)
    except ProgrammingError:
        logger.exception("viewmodel.viewmodel_current")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("viewmodel.current_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while loading viewmodel data.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("viewmodel.current_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load viewmodel data.",
        ) from None
    enabled_features = plan_ctx.list_enabled_features()
    feature_flags = [
        FeatureFlagInfo(
            name=flag.name,
            description=flag.description,
            tier=flag.tier,
            active=flag.currently_active,
        )
        for flag in enabled_features
    ]

    return ViewModelCurrent(
        user=UserInfo(username=current_user.username),
        org=OrganisationInfo(
            org_id=org.id if org else current_user.organisation_id or uuid.uuid4(),
            org_name=org.name if org else "System Admin",
            settings={k: v for k, v in (org.settings_json or {}).items() if k in _SAFE_ORG_SETTINGS_KEYS}
            if org
            else {},
        ),
        org_role=current_user.org_role or "",
        team_memberships=[TeamMembershipInfo(team_id=m.team_id, team_role=m.role) for m in memberships],
        team_memberships_truncated=False,
        preferences=account.preferences,
        feature_flags=feature_flags,
        plan=PlanInfo(
            tier=_resolve_tier(settings, org=org),
            daily_spend_limit=(
                float(org.daily_spend_limit) if org is not None and org.daily_spend_limit is not None else None
            ),
        ),
        pipelines=[PipelineSummary.model_validate(p) for p in (pipelines_page.items if pipelines_page else [])],
        pipelines_total=pipelines_page.total if pipelines_page else 0,
        recent_runs=[RunSummary.model_validate(r) for r in (runs_page.items if runs_page else [])],
        runs_total=runs_page.total if runs_page else 0,
        pending_hitl_gates=[PendingHitlGate.model_validate(h) for h in pending_hitl],
        views=all_views,
        current_view=current_view,
        is_system_admin=current_user.is_system_admin,
    )


@router.get("/api/v1/viewmodel/views")
@handle_db_errors("viewmodel.viewmodel_list_views")
async def viewmodel_list_views(
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
) -> ViewModelViewsResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role or "admin")
            result = await list_views(session, page=page, page_size=page_size)
    except ProgrammingError:
        logger.exception("viewmodel.viewmodel_list_views")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("viewmodel.list_views_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing views.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("viewmodel.list_views_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list views.",
        ) from None

    items = [_enrich_view(v, current_user.account_id) for v in result.items]
    return ViewModelViewsResponse(
        items=items,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        run_list_views=[v for v in items if v.view_type == "run_list"],
        pipeline_list_views=[v for v in items if v.view_type == "pipeline_list"],
        audit_log_views=[v for v in items if v.view_type == "audit_log"],
    )


def _enrich_view(view: SavedView, user_id: uuid.UUID) -> ViewInfo:
    info = ViewInfo.model_validate(view)
    info.created_by_me = view.account_id == user_id
    return info


def _resolve_tier(settings: Settings, org: Any | None = None) -> str:
    from modulo.core.license import get_license, parse_and_verify

    if org is not None:
        org_key = org.settings_json.get("license_key") if hasattr(org, "settings_json") else None
        if org_key:
            validation = parse_and_verify(org_key)
            if validation.valid and validation.license_data is not None:
                return validation.license_data.tier

    lic = get_license()
    if lic is not None:
        return lic.tier
    if settings.modulo_license_key:
        return "team"
    return "community"
