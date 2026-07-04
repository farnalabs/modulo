"""Library contribution REST API — fixture contribution flow."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.routes.library import LibraryPrimitiveResponse
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.library_service import (
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
    contribute_fixture,
    list_contribution_versions,
    list_contributions,
    publish_contribution,
    submit_contribution_for_review,
    submit_contribution_version,
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
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            prim = await contribute_fixture(
                session,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                name=body.name,
                slug=body.slug,
                description=body.description,
                tags=body.tags,
                fixture_map=body.fixture_map,
                source_run_id=(uuid.UUID(body.source_run_id) if body.source_run_id else None),
                source_pipeline_id=(uuid.UUID(body.source_pipeline_id) if body.source_pipeline_id else None),
                owner_team_id=(uuid.UUID(body.owner_team_id) if body.owner_team_id else None),
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
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
            account_id=principal.account_id,
        )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found") from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
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
            approved_by=principal.account_id,
        )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found") from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    return ContributionStatusResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        visibility=prim.visibility,
        name=prim.name,
        slug=prim.slug,
    )


class VersionResponse(BaseModel):
    id: uuid.UUID
    version: str
    contribution_status: str | None
    name: str
    slug: str
    created_by: str | None = Field(default=None, validation_alias="account_id")


class VersionListResponse(BaseModel):
    versions: list[VersionResponse]
    total: int


@router.post("/{primitive_id}/versions", response_model=ContributeFixtureResponse, status_code=status.HTTP_201_CREATED)
async def submit_contribution_version_endpoint(
    primitive_id: uuid.UUID,
    body: ContributeFixtureRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ContributeFixtureResponse:
    """Submit a new version of an existing published fixture contribution.

    Accepts the same fields as creation.  The version string is auto-incremented
    and the new version starts as a draft, going through the same
    review -> publish lifecycle.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            prim = await submit_contribution_version(
                session,
                principal.organisation_id,
                primitive_id,
                account_id=principal.account_id,
                name=body.name,
                slug=body.slug,
                description=body.description,
                tags=body.tags,
                fixture_map=body.fixture_map,
                source_run_id=(uuid.UUID(body.source_run_id) if body.source_run_id else None),
                source_pipeline_id=(uuid.UUID(body.source_pipeline_id) if body.source_pipeline_id else None),
                owner_team_id=(uuid.UUID(body.owner_team_id) if body.owner_team_id else None),
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found") from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    return ContributeFixtureResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        name=prim.name,
        slug=prim.slug,
    )


@router.get("/{primitive_id}/versions", response_model=VersionListResponse)
async def list_contribution_versions_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> VersionListResponse:
    """List all versions for a fixture contribution."""
    try:
        versions = await list_contribution_versions(
            session,
            principal.organisation_id,
            primitive_id,
        )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found") from None
    return VersionListResponse(
        versions=[
            VersionResponse(
                id=v.id,
                version=v.version,
                contribution_status=v.contribution_status,
                name=v.name,
                slug=v.slug,
                account_id=v.account_id.hex if v.account_id else None,
            )
            for v in versions
        ],
        total=len(versions),
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
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_contributions(
                session,
                principal.organisation_id,
                contribution_status=contribution_status,
                page=page,
                page_size=page_size,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    return {
        "items": [LibraryPrimitiveResponse.model_validate(p) for p in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }
