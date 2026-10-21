"""ViewModel aggregate API.

GET /api/v1/me       — current user info (canonical; auth/me also works)
GET /api/v1/viewmodel/current — single-request aggregate for the frontend
GET /api/v1/plan     — caller-scoped effective plan/flag bootstrap (any member)
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import (
    PlanContext,
    effective_feature_flags,
    feature_overrides_from_settings,
    resolve_effective_license,
    resolve_plan_context,
)
from modulo.db.crud.account import get_account_by_id
from modulo.db.crud.base import PageResult
from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.pipeline import list_pipelines
from modulo.db.crud.run import list_runs
from modulo.db.crud.team_membership import list_team_memberships_for_account
from modulo.db.crud.view import get_view, list_views
from modulo.db.models.account import Account
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import HITL_ACTIONABLE_RUN_STATUSES, Run
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.view import SavedView
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.soft_delete import include_soft_deleted
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
    # FAR-1141 / ADR-042: ``/viewmodel/current`` is a claim-ready run surface, so
    # it carries the run's execution origin ('dispatched' / NULL) exactly like the
    # REST list and detail. Additive + nullable: pre-column rows and runs Modulo
    # executed itself read NULL.
    execution_origin: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PendingHitlReview(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    pipeline_id: uuid.UUID
    review_id: str
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


class PlanLicenseInfo(BaseModel):
    """Effective-licence view served by ``GET /api/v1/plan`` (FAR-1806).

    ``tier`` is the plan context's resolved tier; ``expires_at`` / ``org_id``
    come from the same licence-ladder rung the admin ``/api/v1/admin/license``
    resolver walks, so a member reads the licence view without hitting an
    admin-permission-gated endpoint.
    """

    tier: str
    expires_at: str | None = None
    org_id: str | None = None


class PlanFlagInfo(BaseModel):
    """One effective feature flag in the ``/api/v1/plan`` payload.

    Mirrors the admin feature-flag list's ``flags[]`` vocabulary
    (``currently_active`` / ``depends_on`` included) so a client reading
    either endpoint maps the same field names.
    """

    name: str
    description: str
    tier: str
    currently_active: bool
    depends_on: list[str] | None = None


class PlanBootstrapResponse(BaseModel):
    """Caller-scoped effective plan + feature bootstrap (FAR-1806).

    Readable by ANY authenticated member of their own org (no instance-admin
    permission), with the org ``feature_overrides`` overlay applied and the
    FULL known flag set present — inactive flags carry
    ``currently_active: false`` rather than being omitted.
    """

    license: PlanLicenseInfo
    dev_mode: bool
    flags: list[PlanFlagInfo]


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
    pending_hitl_reviews: list[PendingHitlReview]
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
        return _resolve_license_info(settings)
    except Exception:
        logger.exception("license_info")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve license information",
        ) from None


def _resolve_license_info(settings: Settings) -> LicenseInfo:
    """Resolve the public licence view from the deployment-level licence ladder.

    FAR-1720: this endpoint is unauthenticated and org-less, so it mirrors
    rungs 2 and 3 of the shared ``core.feature_flags.resolve_effective_license``
    walk (called with no org, so the org-level rung does not apply):

    1. System-level in-memory licence (``store_license()``) — its tier and
       features are reported, ``is_valid=True``.
    2. System-level env-var licence (``settings.modulo_license_key``) — only
       reported when ``parse_and_verify`` confirms it; an unverifiable key
       falls through (never trusted on mere presence).
    3. No valid licence — ``tier="community"``, no features,
       ``is_valid=False``.

    The response shape stays ``LicenseInfo`` (tier / features / is_valid);
    neither the raw key nor org-scoped fields are exposed.
    """
    data = resolve_effective_license(settings, None)
    if data is None:
        return LicenseInfo(tier="community", features=[], is_valid=False)
    return LicenseInfo(tier=data.tier, features=list(data.features), is_valid=True)


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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "viewmodel.me")
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


def _feature_flag_infos(plan_ctx: Any, org: Any) -> list[FeatureFlagInfo]:
    """The full known flag set with the org ``feature_overrides`` overlay."""
    # FAR-1806: the full known flag set with the org ``feature_overrides``
    # overlay — NOT ``list_enabled_features()`` (active-only, no overlay), which
    # disagreed with the admin endpoint on the same org and dropped default-OFF
    # flags the client needs to resolve chrome. Shared helper:
    # ``core.feature_flags.effective_feature_flags``.
    effective_features = effective_feature_flags(plan_ctx, _org_feature_overrides(org))
    return [
        FeatureFlagInfo(
            name=flag.name,
            description=flag.description,
            tier=flag.tier,
            active=flag.currently_active,
        )
        for flag in effective_features
    ]


def _org_info(org: Any, current_user: AuthenticatedPrincipal) -> OrganisationInfo:
    """The caller's org block, with the system-admin fallback."""
    org_id = org.id if org else current_user.organisation_id or uuid.uuid4()
    org_name = org.name if org else "System Admin"
    settings = {k: v for k, v in (org.settings_json or {}).items() if k in _SAFE_ORG_SETTINGS_KEYS} if org else {}
    return OrganisationInfo(org_id=org_id, org_name=org_name, settings=settings)


def _plan_info(settings: Settings, org: Any) -> PlanInfo:
    """The plan block, including the org's optional daily spend limit."""
    daily_spend_limit = float(org.daily_spend_limit) if org is not None and org.daily_spend_limit is not None else None
    return PlanInfo(tier=_resolve_tier(settings, org=org), daily_spend_limit=daily_spend_limit)


def _pipeline_summaries(pipelines_page: Any) -> tuple[list[PipelineSummary], int]:
    """Validated pipeline summaries plus the page total (empty when absent)."""
    if pipelines_page is None:
        return [], 0
    return [PipelineSummary.model_validate(p) for p in pipelines_page.items], pipelines_page.total


def _run_summaries(runs_page: Any) -> tuple[list[RunSummary], int]:
    """Validated recent-run summaries plus the page total (empty when absent)."""
    if runs_page is None:
        return [], 0
    return [RunSummary.model_validate(r) for r in runs_page.items], runs_page.total


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
        fetched = await _fetch_viewmodel(session, current_user, settings, view_as_team, current_view_id)
    except ProgrammingError:
        logger.exception("viewmodel.viewmodel_current")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "viewmodel.viewmodel_current")
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

    org = fetched.org
    pipelines, pipelines_total = _pipeline_summaries(fetched.pipelines_page)
    recent_runs, runs_total = _run_summaries(fetched.runs_page)
    return ViewModelCurrent(
        user=UserInfo(username=current_user.username),
        org=_org_info(org, current_user),
        org_role=current_user.org_role or "",
        team_memberships=[TeamMembershipInfo(team_id=m.team_id, team_role=m.role) for m in fetched.memberships],
        team_memberships_truncated=False,
        preferences=fetched.account.preferences,
        feature_flags=_feature_flag_infos(fetched.plan_ctx, org),
        plan=_plan_info(settings, org),
        pipelines=pipelines,
        pipelines_total=pipelines_total,
        recent_runs=recent_runs,
        runs_total=runs_total,
        pending_hitl_reviews=[PendingHitlReview.model_validate(h) for h in fetched.pending_hitl],
        views=fetched.all_views,
        current_view=fetched.current_view,
        is_system_admin=current_user.is_system_admin,
    )


@router.get("/api/v1/plan")
@handle_db_errors("viewmodel.plan_bootstrap")
async def plan_bootstrap(
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> PlanBootstrapResponse:
    """Caller-scoped effective plan bootstrap, readable by any authenticated member.

    FAR-1806: the client's plan/tier/feature bootstrap previously read the
    instance-admin-only ``/api/v1/admin/feature-flags`` (and
    ``/api/v1/admin/license``), so every non-admin ran the app on permanently
    wrong defaults (tier ``community``, ``dev_mode`` false, no flags). This
    endpoint serves the same caller-scoped truth — effective tier, dev mode,
    licence view, and the FULL effective flag set with the org
    ``feature_overrides`` overlay — with no permission beyond authentication
    against the caller's own org. The admin endpoints keep their
    system-admin/permission gates for the inspection and toggle UI.

    Failure ladder mirrors ``viewmodel_current``: ProgrammingError -> 501,
    SQLAlchemyError -> 503, HTTPException re-raised, Exception -> 500.
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role or "")
            org = await _resolve_viewmodel_org(session, current_user)
            plan_ctx = await resolve_plan_context(settings, session, org=org)
    except ProgrammingError:
        logger.exception("viewmodel.plan_bootstrap")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "viewmodel.plan_bootstrap")
        logger.exception("viewmodel.plan_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while loading plan data.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("viewmodel.plan_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load plan data.",
        ) from None

    flags = effective_feature_flags(plan_ctx, _org_feature_overrides(org))
    expires_at, license_org_id = _resolve_license_expiry(settings, org)
    return PlanBootstrapResponse(
        license=PlanLicenseInfo(tier=plan_ctx.tier(), expires_at=expires_at, org_id=license_org_id),
        dev_mode=bool(settings.modulo_dev_mode),
        flags=[
            PlanFlagInfo(
                name=flag.name,
                description=flag.description,
                tier=flag.tier,
                currently_active=flag.currently_active,
                depends_on=flag.depends_on,
            )
            for flag in flags
        ],
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "viewmodel.viewmodel_list_views")
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


def _org_feature_overrides(org: Organisation | None) -> dict[str, bool]:
    """The org's ``feature_overrides`` map (bool entries only); ``{}`` without an org.

    The read is in-memory — the caller already loaded the org row — so no
    extra DB round-trip. The bool-only / malformed-settings rule itself lives
    in the shared ``core.feature_flags.feature_overrides_from_settings`` —
    the same reader the admin feature-flag list uses — so this payload and the
    admin endpoint cannot drift: only ``bool`` entries count as an override
    (``1``/``0`` and other truthy junk are ignored), so a malformed settings
    entry can never flip a flag's reported state.
    """
    if org is None:
        return {}
    return feature_overrides_from_settings(getattr(org, "settings_json", None))


def _resolve_license_expiry(settings: Settings, org: Organisation | None) -> tuple[str | None, str | None]:
    """``(expires_at, org_id)`` of the effective licence for ``GET /api/v1/plan``.

    Delegates the rung walk to the shared session-free
    ``core.feature_flags.resolve_effective_license`` — the same walk
    ``admin_license._resolve_effective_license`` and rungs 1-3 of
    ``core.feature_flags._resolve_paid_plan`` use: org-level key, then the
    in-memory licence, then the env-var key — every key verified, never
    trusted on mere presence (FAR-1733). Returns ``(None, None)`` when no
    valid licence resolves.

    Display-only: the payload's ``license.tier`` comes from the plan context,
    which walks the identical rungs, so tier and expiry always describe the
    same licence.
    """
    data = resolve_effective_license(settings, org)
    if data is None:
        return None, None
    return data.expires_at or None, data.org_id or None


def _resolve_tier(settings: Settings, org: Any | None = None) -> str:
    """Resolve the deployment tier for ``/viewmodel/current``'s ``plan.tier``.

    Delegates to the shared canonical licence ladder
    (``core.feature_flags.resolve_effective_license``): org-level key, then
    the in-memory licence, then the env-var key — each key trusted only after
    verification (never on mere presence, FAR-1733) so this resolver cannot
    disagree with ``_resolve_license_info`` / ``_resolve_license_expiry`` for
    the same deployment state. Falls back to ``community``.
    """
    data = resolve_effective_license(settings, org)
    return data.tier if data is not None else "community"


# ---------------------------------------------------------------------------
# /viewmodel/current fetch helpers
# ---------------------------------------------------------------------------


@dataclass
class _ViewModelFetch:
    """DB-fetched inputs for one ``/viewmodel/current`` response."""

    org: Organisation | None
    account: Account
    memberships: list[TeamMembership]
    pipelines_page: PageResult[Pipeline] | None
    runs_page: PageResult[Run] | None
    pending_hitl: list[PendingHitlReview]
    all_views: list[ViewInfo]
    current_view: ViewInfo | None
    plan_ctx: PlanContext


async def _fetch_viewmodel(
    session: AsyncSession,
    current_user: AuthenticatedPrincipal,
    settings: Settings,
    view_as_team: uuid.UUID | None,
    current_view_id: uuid.UUID | None,
) -> _ViewModelFetch:
    """Fetch everything ``/viewmodel/current`` aggregates, inside one transaction.

    ``HTTPException`` raised here (400/404 validation) propagates to the
    endpoint's exception ladder, which re-raises it untouched.
    """
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await set_rls_user_context(session, current_user.account_id, current_user.org_role or "")

        await _require_view_as_team_scope(session, current_user, view_as_team)

        org = await _resolve_viewmodel_org(session, current_user)

        account = await get_account_by_id(session, current_user.account_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

        memberships = await list_team_memberships_for_account(session, current_user.account_id)

        pipelines_page: PageResult[Pipeline] | None
        runs_page: PageResult[Run] | None
        pending_hitl: list[PendingHitlReview]
        all_views: list[ViewInfo]
        current_view: ViewInfo | None
        if current_user.organisation_id is not None:
            pipelines_page, runs_page, pending_hitl, all_views, current_view = await _load_org_dashboard(
                session,
                current_user.organisation_id,
                current_user.account_id,
                memberships,
                current_view_id,
            )
        else:
            pipelines_page = None
            runs_page = None
            pending_hitl = []
            all_views = []
            current_view = None

        plan_ctx = await resolve_plan_context(settings, session, org=org)

    return _ViewModelFetch(
        org=org,
        account=account,
        memberships=memberships,
        pipelines_page=pipelines_page,
        runs_page=runs_page,
        pending_hitl=pending_hitl,
        all_views=all_views,
        current_view=current_view,
        plan_ctx=plan_ctx,
    )


async def _require_view_as_team_scope(
    session: AsyncSession,
    current_user: AuthenticatedPrincipal,
    view_as_team: uuid.UUID | None,
) -> None:
    """Validate a ``view_as_team`` request: 400 without an org, 404 for an unknown team."""
    if view_as_team is None:
        return
    if current_user.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot use view_as_team without an organisation",
        )
    team_result = await session.execute(
        select(Team).where(
            Team.id == view_as_team,
            Team.organisation_id == current_user.organisation_id,
            Team.deleted_at.is_(None),
        )
    )
    if team_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")


async def _resolve_viewmodel_org(
    session: AsyncSession,
    current_user: AuthenticatedPrincipal,
) -> Organisation | None:
    """Load the caller's organisation (404 when missing); system admins may have none."""
    if current_user.organisation_id is None:
        if not current_user.is_system_admin:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
        return None
    org = await get_organisation(session, current_user.organisation_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
    return org


async def _load_org_dashboard(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    account_id: uuid.UUID,
    memberships: list[TeamMembership],
    current_view_id: uuid.UUID | None,
) -> tuple[PageResult[Pipeline], PageResult[Run], list[PendingHitlReview], list[ViewInfo], ViewInfo | None]:
    """Load the org-scoped dashboard slice: pipelines, runs, pending HITL reviews, saved views."""
    pipelines_page = await list_pipelines(session, page=1, page_size=20, organisation_id=organisation_id)
    runs_page = await list_runs(session, organisation_id=organisation_id, page=1, page_size=10)

    user_team_ids = [m.team_id for m in memberships]
    pending_hitl = await _fetch_pending_hitl_reviews(session, organisation_id, user_team_ids)

    all_views_result = await list_views(session, page=1, page_size=100)
    all_views = [_enrich_view(v, account_id) for v in all_views_result.items]

    current_view = None
    if current_view_id is not None:
        view = await get_view(session, current_view_id)
        if view is not None:
            current_view = _enrich_view(view, account_id)

    return pipelines_page, runs_page, pending_hitl, all_views, current_view


async def _fetch_pending_hitl_reviews(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    user_team_ids: list[uuid.UUID],
) -> list[PendingHitlReview]:
    """Pending HITL claims visible to the user, restricted to actionable run statuses.

    FAR-645: join ``runs`` and filter to the actionable run statuses —
    identical semantics to the org pending list
    (``HITLManager.list_pending``). Without the filter, gates on
    terminal runs still rendered on the dashboard and claiming
    them 409'd.
    """
    hitl_query = (
        select(HitlClaim, Team.name.label("required_team_name"))
        .outerjoin(Team, HitlClaim.required_team_id == Team.id)
        .join(Run, HitlClaim.run_id == Run.id)
        .where(
            HitlClaim.organisation_id == organisation_id,
            HitlClaim.decision.is_(None),
            Run.status.in_(HITL_ACTIONABLE_RUN_STATUSES),
        )
    )
    if user_team_ids:
        hitl_query = hitl_query.where(
            HitlClaim.required_team_id.is_(None) | HitlClaim.required_team_id.in_(user_team_ids)
        )
    else:
        hitl_query = hitl_query.where(HitlClaim.required_team_id.is_(None))

    # FAR-1653: include_soft_deleted so a soft-deleted required
    # team's name still renders on this historical read (the
    # outer join would otherwise NULL the label via the global
    # soft-delete filter's ON-clause criteria).
    # ``Select[Any]`` cast: include_soft_deleted's ``T: Select[Any]``
    # bound rejects the concrete ``Select[HitlClaim, str]`` the
    # entity-plus-column select infers (SQLAlchemy's type parameters
    # are invariant) — same pattern as lifecycle_maps/pipelines
    # scope reads.
    pending_hitl_result = await session.execute(include_soft_deleted(cast(Select[Any], hitl_query)))
    return [
        PendingHitlReview(
            id=h.id,
            run_id=h.run_id,
            pipeline_id=h.pipeline_id,
            review_id=h.review_id,
            claimed_by=h.account_id,
            expires_at=h.expires_at,
            required_team_id=h.required_team_id,
            required_team_name=team_name,
        )
        for h, team_name in pending_hitl_result.all()
    ]
