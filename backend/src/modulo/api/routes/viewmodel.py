"""ViewModel aggregate API.

GET /api/v1/me       — current user info (canonical; auth/me also works)
GET /api/v1/viewmodel/current — single-request aggregate for the frontend
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.pipeline import list_pipelines
from modulo.db.crud.run import list_runs
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

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
    preferences: dict = {}


class UpdatePreferencesRequest(BaseModel):
    preferences: dict = Field(default_factory=dict)


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

    model_config = {"from_attributes": True}


class LicenseInfo(BaseModel):
    tier: str = "free"
    features: list[str] = []
    is_valid: bool = True


class ViewModelCurrent(BaseModel):
    user: UserInfo
    pipelines: list[PipelineSummary]
    pipelines_total: int
    recent_runs: list[RunSummary]
    runs_total: int
    pending_hitl_gates: list[PendingHitlGate]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/v1/license", response_model=LicenseInfo)
async def license_info(
    settings: Settings = Depends(get_settings),
) -> LicenseInfo:
    has_license_key = bool(settings.modulo_license_key)
    features: list[str] = []
    if has_license_key:
        features = ["notifications"]
    return LicenseInfo(
        tier="enterprise" if has_license_key else "free",
        features=features,
        is_valid=True,
    )


@router.get("/api/v1/me", response_model=MeResponse)
async def me(current_user: AuthenticatedPrincipal = Depends(get_current_user)) -> MeResponse:
    # Alpha auth is single-org and configured from environment variables. The
    # response already has the v1 shape so frontend hydration does not depend
    # on a hard-coded organisation.
    return MeResponse(
        user=UserInfo(username=current_user.username),
        org=OrganisationInfo(
            org_id=current_user.organisation_id,
            org_name="Modulo",
            settings={},
        ),
        team_memberships=[],
        team_memberships_truncated=False,
        org_role=current_user.org_role,
    )


@router.get("/api/v1/viewmodel/current", response_model=ViewModelCurrent)
async def viewmodel_current(
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> ViewModelCurrent:
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)

        pipelines_page = await list_pipelines(session, page=1, page_size=20)
        runs_page = await list_runs(session, page=1, page_size=10)

        pending_hitl_result = await session.execute(
            select(HitlClaim).where(
                HitlClaim.organisation_id == current_user.organisation_id,
                HitlClaim.decision.is_(None),
            )
        )
        pending_hitl = list(pending_hitl_result.scalars())

    return ViewModelCurrent(
        user=UserInfo(username=current_user.username),
        pipelines=[PipelineSummary.model_validate(p) for p in pipelines_page.items],
        pipelines_total=pipelines_page.total,
        recent_runs=[RunSummary.model_validate(r) for r in runs_page.items],
        runs_total=runs_page.total,
        pending_hitl_gates=[PendingHitlGate.model_validate(h) for h in pending_hitl],
    )
