"""Library contribution REST API — fixture contribution flow."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.routes.library import LibraryPrimitiveResponse
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.library_service import (
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
    contribute_fixture,
    list_contributions,
    publish_contribution,
    submit_contribution_for_review,
)
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/library/contribute", tags=["library-contributions"])


class ContributeFixtureRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    fixture_map: dict[str, str]
    source_run_id: str | None = None
    source_pipeline_id: str | None = None
    owner_team_id: str | None = None


class ContributeFixtureResponse(BaseModel):
    id: uuid.UUID
    contribution_status: str | None
    name: str
    slug: str


class ContributionStatusResponse(BaseModel):
    id: uuid.UUID
    contribution_status: str | None
    visibility: str
    name: str
    slug: str


@router.post("", response_model=ContributeFixtureResponse, status_code=status.HTTP_201_CREATED)
async def create_contribution(
    body: ContributeFixtureRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ContributeFixtureResponse:
    """Submit a test fixture contribution (stored as draft).

    The fixture_map should contain normalized-input -> response pairs suitable
    for StubModelBackend.  The contribution starts in 'draft' status and can
    be moved to review_queue and then published.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        prim = await contribute_fixture(
            session,
            org_id=principal.organisation_id,
            created_by=principal.user_id,
            name=body.name,
            slug=body.slug,
            description=body.description,
            tags=body.tags,
            fixture_map=body.fixture_map,
            source_run_id=(
                uuid.UUID(body.source_run_id) if body.source_run_id else None
            ),
            source_pipeline_id=(
                uuid.UUID(body.source_pipeline_id) if body.source_pipeline_id else None
            ),
            owner_team_id=(
                uuid.UUID(body.owner_team_id) if body.owner_team_id else None
            ),
        )
    return ContributeFixtureResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        name=prim.name,
        slug=prim.slug,
    )


@router.post("/{primitive_id}/submit", response_model=ContributionStatusResponse)
async def submit_for_review(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ContributionStatusResponse:
    """Move a draft contribution to the review queue."""
    try:
        prim = await submit_contribution_for_review(
            session,
            principal.organisation_id,
            primitive_id,
            created_by=principal.user_id,
        )
    except ContributionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found"
        ) from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from None
    return ContributionStatusResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        visibility=prim.visibility,
        name=prim.name,
        slug=prim.slug,
    )


@router.post("/{primitive_id}/publish", response_model=ContributionStatusResponse)
async def publish_contribution_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ContributionStatusResponse:
    """Publish a reviewed fixture contribution to the community library.

    Only org owners/admins may publish contributions.  This endpoint requires
    the user to have org_role='admin' or org_role='owner'.
    """
    if principal.org_role not in ("admin", "owner"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only org owners and admins may publish contributions",
        )

    try:
        prim = await publish_contribution(
            session,
            principal.organisation_id,
            primitive_id,
            approved_by=principal.user_id,
        )
    except ContributionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found"
        ) from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from None
    return ContributionStatusResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        visibility=prim.visibility,
        name=prim.name,
        slug=prim.slug,
    )


@router.get("", response_model=dict[str, object])
async def list_contributions_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contribution_status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, object]:
    """List fixture contributions visible to the current org."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await list_contributions(
            session,
            principal.organisation_id,
            contribution_status=contribution_status,
            page=page,
            page_size=page_size,
        )
    return {
        "items": [
            LibraryPrimitiveResponse.model_validate(p) for p in result.items
        ],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }
