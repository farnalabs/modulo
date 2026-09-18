"""Library contribution REST API — fixture contribution flow."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.api.routes.library import LibraryPrimitiveResponse
from modulo.auth.jwt import TenantPrincipal
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
from modulo.db.rls import set_rls_org, set_rls_user_context

_MSG_CONTRIBUTION_FEATURE_TEMPORARILY_UNAVAILABLE = (
    "The contribution feature is temporarily unavailable due to a database issue. Please retry."
)
_MSG_CONTRIBUTION_NOT_FOUND = "Contribution not found"


router = APIRouter(prefix="/api/v1/library/contribute", tags=["library-contributions"])

_log = logging.getLogger(__name__)


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


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("contributions.create_contribution")
async def create_contribution(
    req: ContributeFixtureRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("contribution.create"),
) -> ContributeFixtureResponse:
    """Submit a test fixture contribution (stored as draft).

    The fixture_map should contain normalized-input -> response pairs suitable
    for StubModelBackend.  The contribution starts in 'draft' status and can
    be moved to review_queue and then published.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            prim = await contribute_fixture(
                session,
                org_id=principal.organisation_id,
                created_by=principal.account_id,
                name=req.name,
                slug=req.slug,
                description=req.description,
                tags=req.tags,
                fixture_map=req.fixture_map,
                source_run_id=(uuid.UUID(req.source_run_id) if req.source_run_id else None),
                source_pipeline_id=(uuid.UUID(req.source_pipeline_id) if req.source_pipeline_id else None),
                owner_team_id=(uuid.UUID(req.owner_team_id) if req.owner_team_id else None),
            )
    except ProgrammingError:
        _log.exception("contributions.create_contribution")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("create_contribution: SQLAlchemyError")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_CONTRIBUTION_FEATURE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("create_contribution: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the contribution.",
        ) from None
    return ContributeFixtureResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        name=prim.name,
        slug=prim.slug,
    )


@router.post("/{primitive_id}/submit")
@handle_db_errors("contributions.submit_for_review")
async def submit_for_review(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("contribution.submit"),
) -> ContributionStatusResponse:
    """Move a draft contribution to the review queue."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            prim = await submit_contribution_for_review(
                session,
                principal.organisation_id,
                primitive_id,
                _created_by=principal.account_id,
            )
    except ProgrammingError:
        _log.exception("contributions.submit_for_review")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CONTRIBUTION_NOT_FOUND) from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    except SQLAlchemyError:
        _log.exception("submit_for_review: SQLAlchemyError")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_CONTRIBUTION_FEATURE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("submit_for_review: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while submitting the contribution for review.",
        ) from None
    return ContributionStatusResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        visibility=prim.visibility,
        name=prim.name,
        slug=prim.slug,
    )


@router.post("/{primitive_id}/publish")
@handle_db_errors("contributions.publish_contribution_endpoint")
async def publish_contribution_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("contribution.publish"),
) -> ContributionStatusResponse:
    """Publish a reviewed fixture contribution to the community library.

    Only org admins may publish contributions (``contribution.publish``).
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            prim = await publish_contribution(
                session,
                principal.organisation_id,
                primitive_id,
            )
    except ProgrammingError:
        _log.exception("contributions.publish_contribution_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CONTRIBUTION_NOT_FOUND) from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    except SQLAlchemyError:
        _log.exception("publish_contribution_endpoint: SQLAlchemyError")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_CONTRIBUTION_FEATURE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("publish_contribution_endpoint: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while publishing the contribution.",
        ) from None
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


@router.post("/{primitive_id}/versions", status_code=status.HTTP_201_CREATED)
@handle_db_errors("contributions.submit_contribution_version_endpoint")
async def submit_contribution_version_endpoint(
    primitive_id: uuid.UUID,
    req: ContributeFixtureRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("contribution.version"),
) -> ContributeFixtureResponse:
    """Submit a new version of an existing published fixture contribution.

    Accepts the same fields as creation.  The version string is auto-incremented
    and the new version starts as a draft, going through the same
    review -> publish lifecycle.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            prim = await submit_contribution_version(
                session,
                principal.organisation_id,
                primitive_id,
                created_by=principal.account_id,
                name=req.name,
                slug=req.slug,
                description=req.description,
                tags=req.tags,
                fixture_map=req.fixture_map,
                source_run_id=(uuid.UUID(req.source_run_id) if req.source_run_id else None),
                source_pipeline_id=(uuid.UUID(req.source_pipeline_id) if req.source_pipeline_id else None),
                owner_team_id=(uuid.UUID(req.owner_team_id) if req.owner_team_id else None),
            )
    except ProgrammingError:
        _log.exception("contributions.submit_contribution_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CONTRIBUTION_NOT_FOUND) from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from None
    except SQLAlchemyError:
        _log.exception("submit_contribution_version_endpoint: SQLAlchemyError")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_CONTRIBUTION_FEATURE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("submit_contribution_version_endpoint: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while submitting the contribution version.",
        ) from None
    return ContributeFixtureResponse(
        id=prim.id,
        contribution_status=prim.contribution_status,
        name=prim.name,
        slug=prim.slug,
    )


@router.get("/{primitive_id}/versions")
@handle_db_errors("contributions.list_contribution_versions_endpoint")
async def list_contribution_versions_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("contribution.list"),
) -> VersionListResponse:
    """List all versions for a fixture contribution."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            versions = await list_contribution_versions(
                session,
                principal.organisation_id,
                primitive_id,
            )
    except ProgrammingError:
        _log.exception("contributions.list_contribution_versions_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except ContributionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CONTRIBUTION_NOT_FOUND) from None
    except SQLAlchemyError:
        _log.exception("list_contribution_versions_endpoint: SQLAlchemyError")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_CONTRIBUTION_FEATURE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_contribution_versions_endpoint: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing contribution versions.",
        ) from None
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


@router.get("")
@handle_db_errors("contributions.list_contributions_endpoint")
async def list_contributions_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contribution_status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("contribution.list"),
) -> dict[str, object]:
    """List fixture contributions visible to the current org."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_contributions(
                session,
                principal.organisation_id,
                contribution_status=contribution_status,
                page=page,
                page_size=page_size,
            )
    except ProgrammingError:
        _log.exception("contributions.list_contributions_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("list_contributions_endpoint: SQLAlchemyError")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_CONTRIBUTION_FEATURE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_contributions_endpoint: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing contributions.",
        ) from None
    return {
        "items": [LibraryPrimitiveResponse.model_validate(p) for p in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }
