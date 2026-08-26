"""Library primitive REST API — browse, export, import, rate."""

import copy
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Self, cast

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_in_dev_operator, require_permission
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.api.routes.lifecycle_maps import LifecycleMapResponse
from modulo.auth.dependencies import get_current_tenant_user, require_system_admin
from modulo.auth.jwt import TenantPrincipal
from modulo.core.library_service import (
    CommunityPrimitiveReadOnlyError,
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
    contribute_primitive,
    copy_to_adapt,
    get_primitive,
    get_primitive_by_slug,
    list_org_contributions,
    list_primitives,
    publish_contribution,
)
from modulo.core.lifecycle_map.import_export import (
    PRIMITIVE_TYPE as LIFECYCLE_MAP_PRIMITIVE_TYPE,
)
from modulo.core.lifecycle_map.import_export import (
    LifecycleMapBundleError,
    materialize_map_from_primitive,
)
from modulo.core.lifecycle_map.validation import LifecycleMapContentError, LifecycleMapPipelineConflictError
from modulo.core.workflow_import_export import (
    export_pipeline_bundle,
    export_pipeline_bundle_v2,
    extract_bundle_json_from_zip,
    get_existing_agent_names,
    get_existing_pipeline_names,
    materialize_import,
    resolve_connector_type,
    resolve_model_backend,
    resolve_schema,
    suggest_import_name,
)
from modulo.db.crud.base import PageResult
from modulo.db.crud.library_primitive import (
    create_library_primitive,
    restore_library_primitive,
    soft_delete_library_primitive,
    update_library_primitive,
)
from modulo.db.crud.pipeline import (
    create_pipeline,
    get_pipeline,
)
from modulo.db.crud.rating import (
    CopyToAdaptError,
    DuplicateRatingError,
    RatingCooldownError,
    SelfRatingError,
    get_rating_aggregate,
    list_ratings_for_primitive,
    submit_abuse_report,
    submit_rating,
    update_primitive_ratings_aggregate,
)
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.util import sanitise_log_value as _sanitise_log_value

_MSG_LIBRARY_FEATURE_TEMPORARILY_UNAVAILABLE = (
    "The library feature is temporarily unavailable due to a database issue. Please retry."
)
_CODE_LIBRARY_MANAGE = "library.manage"
_CODE_LIBRARY_CREATE_PIPELINE_TEMPLATE = "library.create_pipeline_from_template_endpoint"
_MSG_ORGANISATION_ID_REQUIRED = "Organisation ID required"


_MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024  # 50 MB

router = APIRouter(prefix="/api/v1/libraries", tags=["libraries"])


def _split_primitive_types(raw: str | None) -> list[str] | None:
    """Split a comma-separated ``primitive_types`` query value into a list.

    Returns ``None`` when the value is empty so callers can treat it as
    "no type filter".
    """
    if not raw:
        return None
    types = [t.strip() for t in raw.split(",") if t.strip()]
    return types or None


def _conflict_error() -> HTTPException:
    """HTTP 409 for a resource that already exists (e.g. slug collision)."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=MSG_RESOURCE_ALREADY_EXISTS,
    )


def _not_implemented_error() -> HTTPException:
    """HTTP 501 when the library tables/migrations are not yet present."""
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=MSG_FEATURE_NOT_AVAILABLE,
    )


def _unavailable_error() -> HTTPException:
    """HTTP 503 for a transient database failure while serving the library."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_MSG_LIBRARY_FEATURE_TEMPORARILY_UNAVAILABLE,
    )


async def _set_rls_context(session: AsyncSession, principal: TenantPrincipal) -> None:
    """Establish the org + user RLS context for a library transaction."""
    await set_rls_org(session, principal.organisation_id)
    await set_rls_user_context(session, principal.account_id, principal.org_role)


@dataclass(frozen=True)
class _PrimitiveListQuery:
    """Grouped query parameters for the library browse endpoint."""

    page: int
    page_size: int
    cursor: str | None
    primitive_type: str | None
    primitive_types: str | None
    search: str | None
    source: str | None
    include_in_dev: bool


@dataclass(frozen=True)
class _BundleResolution:
    """Collected state produced by resolving an import bundle."""

    pipeline_name: str
    warnings: list[str]
    resolved_schemas: list[dict[str, Any]]
    resolved_connectors: list[dict[str, Any]]
    resolved_model_backends: list[dict[str, Any]]
    name_conflicts: list[dict[str, str]]
    available_teams: list[dict[str, Any]]


def _require_organisation_id(principal: TenantPrincipal) -> uuid.UUID:
    """Return the principal's organisation id or reject the request."""
    if principal.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_MSG_ORGANISATION_ID_REQUIRED,
        )
    return principal.organisation_id


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def _trust_tier_for(source: str, verified: bool | None) -> str | None:
    """Compute the trust tier label from a primitive's provenance."""
    if source == "modulo":
        return "modulo"
    if source == "registry" and verified is True:
        return "green"
    if source == "registry":
        return "amber"
    return None


class LibraryPrimitiveResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    source: str
    primitive_type: str
    name: str
    slug: str
    description: str | None
    author: str
    version: str
    tags: list[str]
    content_json: dict[str, Any]
    source_url: str | None
    forked_from: uuid.UUID | None
    checksum: str | None
    ed25519_signature: str | None
    verified: bool | None
    trust_tier: str | None = None
    tier: str = "native"
    download_count: int | None
    average_rating: float | None
    review_count: int | None
    owner_team_id: uuid.UUID | None
    visibility: str
    created_by: uuid.UUID | None = Field(default=None, validation_alias="account_id")
    auto_update: bool = True
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}

    @model_validator(mode="after")
    def _compute_trust_tier(self) -> Self:
        self.trust_tier = _trust_tier_for(self.source, self.verified)
        return self


class LibraryPrimitiveListResponse(BaseModel):
    items: list[LibraryPrimitiveResponse]
    total: int
    page: int
    page_size: int
    next_cursor: str | None = None
    has_more: bool = False


class LibraryPrimitiveCreate(TeamVisibilityMixin):
    primitive_type: str = Field(pattern=r"^(schema|workflow|agent|integration|test_fixture|composite|lifecycle_map)$")
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list)
    content_json: dict[str, Any]
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern=r"^(org|team)$")
    tier: Literal["native", "preview", "in_dev"] = Field(default="native")


class LibraryPrimitiveUpdate(TeamVisibilityMixin):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None
    content_json: dict[str, Any] | None = None
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = Field(default=None, pattern=r"^(org|team)$")
    auto_update: bool | None = None
    tier: Literal["native", "preview", "in_dev"] | None = None


class RatingSubmit(BaseModel):
    thumbs_up: bool
    comment: str | None = Field(default=None, max_length=2000)


class RatingResponse(BaseModel):
    id: uuid.UUID
    primitive_id: uuid.UUID
    user_id: uuid.UUID | None
    thumbs_up: bool
    comment: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RatingAggregateResponse(BaseModel):
    average_rating: float | None
    review_count: int


class RatingListResponse(BaseModel):
    items: list[RatingResponse]
    total: int


class AbuseReportSubmit(BaseModel):
    rating_id: uuid.UUID | None = None
    reason: str = Field(..., min_length=10, max_length=500)


class AbuseReportResponse(BaseModel):
    id: uuid.UUID
    primitive_id: uuid.UUID
    rating_id: uuid.UUID | None
    reporter_user_id: uuid.UUID | None
    reason: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportBundleResponse(BaseModel):
    warnings: list[str] = Field(default_factory=list)
    pipeline_name: str
    bundle_json: str = ""
    resolved_schemas: list[dict[str, Any]] = Field(default_factory=list)
    resolved_connectors: list[dict[str, Any]] = Field(default_factory=list)
    resolved_model_backends: list[dict[str, Any]] = Field(default_factory=list)
    name_conflicts: list[dict[str, str]] = Field(default_factory=list)
    available_teams: list[dict[str, Any]] = Field(default_factory=list)


class ImportConfirmRequest(BaseModel):
    bundle_json: str
    owner_team_id: uuid.UUID | None = None
    schema_overrides: dict[str, str] | None = None
    schema_version_overrides: dict[str, str] | None = None
    connector_overrides: dict[str, str] | None = None
    model_backend_overrides: dict[str, str] | None = None
    pipeline_name_override: str | None = None


class CopyToAdaptRequest(BaseModel):
    target_team_id: uuid.UUID | None = None


class AnalyseBundleRequest(BaseModel):
    bundle: dict[str, Any]


class CreatePipelineFromTemplateRequest(BaseModel):
    name: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Overrides the template's default pipeline name",
    )
    description: str | None = Field(
        None,
        max_length=2000,
        description="Overrides the template's default description",
    )


class PipelineFromTemplateResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    visibility: str
    template_source_id: uuid.UUID
    agent_count: int
    edge_count: int
    ready_to_run: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# List / Browse
# ---------------------------------------------------------------------------


_log = logging.getLogger(__name__)


@router.get("")
@handle_db_errors("library.list_library_primitives_endpoint")
async def list_library_primitives_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    primitive_type: str | None = None,
    primitive_types: str | None = None,
    search: str | None = None,
    source: str | None = None,
    include_in_dev: bool = Query(default=False, description="Include in_dev tier items (default excludes them)"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("library.search"),
) -> LibraryPrimitiveListResponse:
    if include_in_dev:
        require_in_dev_operator(principal, "library.search.in_dev")
    query = _PrimitiveListQuery(
        page=page,
        page_size=page_size,
        cursor=cursor,
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
        source=source,
        include_in_dev=include_in_dev,
    )
    result = await _fetch_primitives(session, principal, query)
    items = _validate_primitive_items(result.items)
    return _build_primitive_list_response(items, result)


def _validate_primitive_items(items: list[LibraryPrimitive]) -> list[LibraryPrimitiveResponse]:
    """Validate ORM primitives into response models, mapping failures to HTTP 500."""
    try:
        return [LibraryPrimitiveResponse.model_validate(p) for p in items]
    except Exception:
        _log.exception("LibraryPrimitiveResponse.model_validate failed on %d items", len(items))
        if items:
            _log.exception(
                "first item type=%s id=%s",
                type(items[0]).__name__,
                getattr(items[0], "id", "?"),
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to parse library primitives. The schema may be out of sync with the database.",
        ) from None


def _build_primitive_list_response(
    items: list[LibraryPrimitiveResponse],
    result: PageResult[LibraryPrimitive],
) -> LibraryPrimitiveListResponse:
    """Build the paged list response, mapping construction failures to HTTP 500."""
    try:
        return LibraryPrimitiveListResponse(
            items=items,
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            next_cursor=result.next_cursor,
            has_more=result.has_more,
        )
    except Exception:
        _log.exception("LibraryPrimitiveListResponse construction failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build library primitives response.",
        ) from None


async def _fetch_primitives(
    session: AsyncSession,
    principal: TenantPrincipal,
    query: _PrimitiveListQuery,
) -> PageResult[LibraryPrimitive]:
    """Query the library primitives list, translating DB failures to HTTP errors."""
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            include_community = query.source != "local"
            return await list_primitives(
                session,
                principal.organisation_id,
                primitive_type=query.primitive_type,
                primitive_types=_split_primitive_types(query.primitive_types),
                search=query.search,
                page=query.page,
                page_size=query.page_size,
                include_community=include_community,
                source=query.source,
                cursor=query.cursor,
                excluded_tiers=[] if query.include_in_dev else None,
            )
    except ProgrammingError:
        _log.exception("library.list_library_primitives_endpoint")
        _log.warning(
            "list_library_primitives_endpoint: ProgrammingError — missing DB table or migration",
            exc_info=True,
        )
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("list_library_primitives_endpoint: SQLAlchemyError — transient DB failure")
        raise _unavailable_error() from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_library_primitives_endpoint: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing library primitives.",
        ) from None


@router.get("/ping")
@handle_db_errors("library.ping")
async def ping() -> dict[str, bool]:
    return {"pong": True}


@router.get("/{primitive_id}")
@handle_db_errors("library.get_library_primitive_endpoint")
async def get_library_primitive_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("library.search"),
) -> LibraryPrimitiveResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            primitive = await get_primitive(session, principal.organisation_id, primitive_id)
    except IntegrityError:
        _log.exception("library.get_library_primitive_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.get_library_primitive_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("get_library_primitive_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    if primitive is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )
    return LibraryPrimitiveResponse.model_validate(primitive)


# ---------------------------------------------------------------------------
# Create / Update / Delete
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("library.create_library_primitive_endpoint")
async def create_library_primitive_endpoint(
    req: LibraryPrimitiveCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> LibraryPrimitiveResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            existing = await get_primitive_by_slug(session, principal.organisation_id, req.primitive_type, req.slug)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Primitive with type '{req.primitive_type}' and slug '{req.slug}' already exists",
                )
            prim = await create_library_primitive(
                session,
                org_id=principal.organisation_id,
                source="local",
                primitive_type=req.primitive_type,
                name=req.name,
                slug=req.slug,
                description=req.description,
                author=principal.account_id.hex,
                version="1.0",
                tags=req.tags,
                content_json=req.content_json,
                source_url=None,
                forked_from=None,
                checksum=None,
                ed25519_signature=None,
                verified=None,
                download_count=None,
                average_rating=None,
                review_count=None,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
                account_id=principal.account_id,
                tier=req.tier,
            )
    except IntegrityError:
        _log.exception("library.create_library_primitive_endpoint")
        _log.warning(
            "create_library_primitive_endpoint: IntegrityError — slug collision on %s/%s",
            _sanitise_log_value(req.primitive_type),
            _sanitise_log_value(req.slug),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Primitive with type '{req.primitive_type}' and slug '{req.slug}' already exists",
        ) from None
    except ProgrammingError:
        _log.exception("library.create_library_primitive_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("create_library_primitive_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return LibraryPrimitiveResponse.model_validate(prim)


@router.patch("/{primitive_id}")
@handle_db_errors("library.update_library_primitive_endpoint")
async def update_library_primitive_endpoint(
    primitive_id: uuid.UUID,
    req: LibraryPrimitiveUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> LibraryPrimitiveResponse:
    updates = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            prim = await update_library_primitive(session, primitive_id, updates)
    except IntegrityError:
        _log.exception("library.update_library_primitive_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.update_library_primitive_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("update_library_primitive_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    if prim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )
    return LibraryPrimitiveResponse.model_validate(prim)


@router.delete("/{primitive_id}")
@handle_db_errors("library.delete_library_primitive_endpoint")
async def delete_library_primitive_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> LibraryPrimitiveResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            prim = await soft_delete_library_primitive(session, primitive_id)
    except IntegrityError:
        _log.exception("library.delete_library_primitive_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.delete_library_primitive_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("delete_library_primitive_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    if prim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )
    return LibraryPrimitiveResponse.model_validate(prim)


@router.post("/{primitive_id}/restore")
@handle_db_errors("library.restore_library_primitive_endpoint")
async def restore_library_primitive_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> LibraryPrimitiveResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            prim = await restore_library_primitive(session, primitive_id)
    except ProgrammingError:
        _log.exception("library.restore_library_primitive_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("restore_library_primitive_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    if prim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found or not deleted",
        )
    return LibraryPrimitiveResponse.model_validate(prim)


# ---------------------------------------------------------------------------
# Copy-to-adapt
# ---------------------------------------------------------------------------


@router.post("/{primitive_id}/adapt")
@handle_db_errors("library.copy_to_adapt_endpoint")
async def copy_to_adapt_endpoint(
    primitive_id: uuid.UUID,
    req: CopyToAdaptRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("library.copy"),
) -> LibraryPrimitiveResponse:
    try:
        result = await copy_to_adapt(
            session,
            principal.organisation_id,
            primitive_id,
            target_team_id=req.target_team_id,
            created_by=principal.account_id,
            org_role=principal.org_role,
            via_mcp=False,
        )
    except CommunityPrimitiveReadOnlyError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Community primitives may only be adapted via the browser UI, not via MCP.",
        ) from None
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        ) from None
    except IntegrityError:
        _log.exception("library.copy_to_adapt_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.copy_to_adapt_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("copy_to_adapt_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return LibraryPrimitiveResponse.model_validate(result)


# ---------------------------------------------------------------------------
# Workflow export
# ---------------------------------------------------------------------------


@router.post("/export/{pipeline_id}")
@handle_db_errors("library.export_pipeline_endpoint")
async def export_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    format: str = Query("v1", pattern="^(v1|v2)$"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> Response:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await get_pipeline(session, pipeline_id, organisation_id=principal.organisation_id)
            if pipeline is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Pipeline {pipeline_id} not found",
                )
            if format == "v2":
                yaml_str = await export_pipeline_bundle_v2(session, pipeline_id)
            else:
                bundle_bytes = await export_pipeline_bundle(session, pipeline_id)
    except IntegrityError:
        _log.exception("library.export_pipeline_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.export_pipeline_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("export_pipeline_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in pipeline.name)
    if format == "v2":
        return Response(
            content=yaml_str,
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}.modulo.yaml"',
            },
        )
    return Response(
        content=bundle_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.modulo.zip"',
        },
    )


# ---------------------------------------------------------------------------
# Workflow import — step 1: analyse bundle
# ---------------------------------------------------------------------------


async def _analyse_bundle(
    session: AsyncSession,
    principal: TenantPrincipal,
    bundle: dict[str, Any],
) -> ImportBundleResponse:
    """Shared analysis logic — validates a bundle and returns resolution state."""
    bundle = copy.deepcopy(bundle)  # avoid mutating caller's dict
    try:
        resolution = await _resolve_import_bundle(session, principal, bundle)
    except IntegrityError:
        _log.exception("library._analyse_bundle")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.warning("_analyse_bundle: ProgrammingError — missing DB table or migration", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.warning("_analyse_bundle: SQLAlchemyError — database connection failure", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed. Please try again later.",
        ) from None

    return ImportBundleResponse(
        warnings=resolution.warnings,
        pipeline_name=resolution.pipeline_name,
        bundle_json=json.dumps(bundle, default=str),
        resolved_schemas=resolution.resolved_schemas,
        resolved_connectors=resolution.resolved_connectors,
        resolved_model_backends=resolution.resolved_model_backends,
        name_conflicts=resolution.name_conflicts,
        available_teams=resolution.available_teams,
    )


async def _resolve_import_bundle(
    session: AsyncSession,
    principal: TenantPrincipal,
    bundle: dict[str, Any],
) -> _BundleResolution:
    """Resolve every reference in the bundle inside one RLS-scoped transaction."""
    warnings: list[str] = []
    resolved_schemas: list[dict[str, Any]] = []
    resolved_connectors: list[dict[str, Any]] = []
    resolved_model_backends_list: list[dict[str, Any]] = []
    name_conflicts: list[dict[str, str]] = []

    async with session.begin():
        await _set_rls_context(session, principal)

        pipeline_info = bundle.get("pipeline", {})
        pipeline_name = pipeline_info.get("name", "Unnamed Pipeline")
        await _resolve_pipeline_name_conflict(
            session,
            principal,
            pipeline_name,
            name_conflicts,
            warnings,
        )

        await _resolve_bundle_schemas(
            session,
            principal,
            bundle,
            resolved_schemas,
            warnings,
        )

        connector_instance_map = await _resolve_bundle_connectors(
            session,
            principal,
            bundle,
            resolved_connectors,
            warnings,
        )

        mb_id_by_name = await _resolve_bundle_model_backends(
            session,
            principal,
            bundle,
            resolved_model_backends_list,
            warnings,
        )

        _warn_duplicate_agent_names(bundle, warnings)
        await _resolve_existing_agent_conflicts(
            session,
            principal,
            bundle,
            name_conflicts,
            warnings,
        )
        _bind_connector_instances_to_graph(pipeline_info, connector_instance_map)
        _bind_model_backends_to_agents(bundle, mb_id_by_name)

        teams = await _fetch_available_teams(session, principal)

    available_teams = [{"id": str(t.id), "name": t.name} for t in teams]

    return _BundleResolution(
        pipeline_name=pipeline_name,
        warnings=warnings,
        resolved_schemas=resolved_schemas,
        resolved_connectors=resolved_connectors,
        resolved_model_backends=resolved_model_backends_list,
        name_conflicts=name_conflicts,
        available_teams=available_teams,
    )


async def _fetch_available_teams(
    session: AsyncSession,
    principal: TenantPrincipal,
) -> list[Team]:
    """Return the org's non-deleted teams for the import team picker."""
    teams_result = await session.execute(
        select(Team).where(
            Team.organisation_id == principal.organisation_id,
            Team.deleted_at.is_(None),
        )
    )
    return list(teams_result.scalars())


async def _resolve_pipeline_name_conflict(
    session: AsyncSession,
    principal: TenantPrincipal,
    pipeline_name: str,
    name_conflicts: list[dict[str, str]],
    warnings: list[str],
) -> None:
    """Warn when the bundle's pipeline name already exists in the org."""
    existing_pipeline_names = await get_existing_pipeline_names(session, principal.organisation_id)
    if pipeline_name in existing_pipeline_names:
        suggested = suggest_import_name(existing_pipeline_names, pipeline_name)
        name_conflicts.append(
            {
                "type": "pipeline",
                "original": pipeline_name,
                "suggested": suggested,
            }
        )
        warnings.append(f"Pipeline '{pipeline_name}' already exists. Suggested: '{suggested}'.")


async def _resolve_bundle_schemas(
    session: AsyncSession,
    principal: TenantPrincipal,
    bundle: dict[str, Any],
    resolved_schemas: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    """Resolve every schema in the bundle, stamping resolved ids onto the bundle."""
    for schema in bundle.get("schemas", []):
        result = await resolve_schema(session, principal.organisation_id, schema)
        resolved_schemas.append(result)
        if result.get("schema_id"):
            schema["_resolved_id"] = result["schema_id"]
            schema["_resolved_version"] = result["version"]
        if result.get("warning"):
            warnings.append(result["warning"])


async def _resolve_bundle_connectors(
    session: AsyncSession,
    principal: TenantPrincipal,
    bundle: dict[str, Any],
    resolved_connectors: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, str]:
    """Resolve the distinct connector types referenced by bundle agents."""
    seen_connector_types: set[str] = set()
    connector_instance_map: dict[str, str] = {}
    for agent in bundle.get("agents", []):
        for ref in agent.get("connector_type_refs", []):
            ctid = ref.get("connector_type_id", ref.get("type", ""))
            if ctid and ctid not in seen_connector_types:
                seen_connector_types.add(ctid)
                result = await resolve_connector_type(session, principal.organisation_id, ctid)
                resolved_connectors.append(result)
                if result.get("instance_id"):
                    connector_instance_map[ctid] = result["instance_id"]
                if result.get("warning"):
                    warnings.append(result["warning"])
    return connector_instance_map


async def _resolve_bundle_model_backends(
    session: AsyncSession,
    principal: TenantPrincipal,
    bundle: dict[str, Any],
    resolved_model_backends: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, str]:
    """Resolve every model backend in the bundle, mapping name -> backend id."""
    for mb in bundle.get("model_backends", []):
        result = await resolve_model_backend(session, principal.organisation_id, mb)
        resolved_model_backends.append(result)
        if result.get("model_backend_id"):
            mb["_resolved_model_backend_id"] = result["model_backend_id"]
        if result.get("warning"):
            warnings.append(result["warning"])

    mb_id_by_name: dict[str, str] = {}
    for mb in bundle.get("model_backends", []):
        rid = mb.get("_resolved_model_backend_id")
        if rid:
            mb_id_by_name[mb.get("name", "")] = rid
    return mb_id_by_name


def _warn_duplicate_agent_names(bundle: dict[str, Any], warnings: list[str]) -> None:
    """Warn when two agents inside the bundle share the same name."""
    agent_names_in_bundle = [a.get("name", "") for a in bundle.get("agents", [])]
    seen_names: set[str] = set()
    for aname in agent_names_in_bundle:
        if aname and aname in seen_names:
            warnings.append(f"Duplicate agent name '{aname}' found in bundle. Each agent must have a unique name.")
        if aname:
            seen_names.add(aname)


async def _resolve_existing_agent_conflicts(
    session: AsyncSession,
    principal: TenantPrincipal,
    bundle: dict[str, Any],
    name_conflicts: list[dict[str, str]],
    warnings: list[str],
) -> None:
    """Warn when a bundle agent's name collides with an existing org agent."""
    existing_agent_names = await get_existing_agent_names(session, principal.organisation_id)
    for agent in bundle.get("agents", []):
        aname = agent.get("name", "")
        if aname in existing_agent_names:
            suggested = suggest_import_name(existing_agent_names, aname)
            name_conflicts.append(
                {
                    "type": "agent",
                    "original": aname,
                    "suggested": suggested,
                }
            )
            warnings.append(f"Agent '{aname}' already exists. Suggested: '{suggested}'.")


def _bind_connector_instances_to_graph(
    pipeline_info: dict[str, Any],
    connector_instance_map: dict[str, str],
) -> None:
    """Stamp resolved connector instance ids onto graph node bindings."""
    for node in pipeline_info.get("graph_nodes_json", []):
        binding = node.get("connector_binding", {})
        if isinstance(binding, dict):
            ctid = binding.get("connector_type_id", "")
            if ctid and ctid in connector_instance_map:
                binding["instance_id"] = connector_instance_map[ctid]


def _bind_model_backends_to_agents(
    bundle: dict[str, Any],
    mb_id_by_name: dict[str, str],
) -> None:
    """Stamp resolved model backend ids onto bundle agents by name."""
    for agent in bundle.get("agents", []):
        mb_name = agent.get("model_backend_name", "")
        if mb_name and mb_name in mb_id_by_name:
            agent["model_backend_id"] = mb_id_by_name[mb_name]


async def _read_zip_upload(file: UploadFile) -> bytes:
    """Validate and read an uploaded .zip file, enforcing the size limit."""
    name = file.filename or ""
    if not name.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .zip or .modulo.zip files are accepted",
        )
    if file.size and file.size > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Upload size exceeds maximum of {_MAX_UPLOAD_SIZE // (1024 * 1024)} MB",
        )
    zip_bytes = await file.read()
    if len(zip_bytes) > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Upload size exceeds maximum of {_MAX_UPLOAD_SIZE // (1024 * 1024)} MB",
        )
    return zip_bytes


@router.post("/import/upload-zip")
@handle_db_errors("library.upload_zip_and_analyse_endpoint")
async def upload_zip_and_analyse_endpoint(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> ImportBundleResponse:
    """Upload a .modulo.zip file, extract bundle.json, and return analysis.

    Replaces the client-side ZIP parsing for a reliable server-side extraction.
    """
    zip_bytes = await _read_zip_upload(file)
    try:
        bundle = extract_bundle_json_from_zip(zip_bytes)
    except (LookupError, json.JSONDecodeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None

    return await _analyse_bundle(session, principal, bundle)


@router.post("/import/analyse")
@handle_db_errors("library.analyse_import_bundle_endpoint")
async def analyse_import_bundle_endpoint(
    req: AnalyseBundleRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> ImportBundleResponse:
    """Analyse a bundle JSON and return resolution warnings + available teams.

    Accepts raw JSON body with bundle content, or use /import/upload-zip
    to upload a .modulo.zip file directly.
    """
    return await _analyse_bundle(session, principal, req.bundle)


# ---------------------------------------------------------------------------
# Workflow import — step 2: confirm and materialize
# ---------------------------------------------------------------------------


@router.post("/import/confirm")
@handle_db_errors("library.confirm_import_endpoint")
async def confirm_import_endpoint(
    req: ImportConfirmRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> dict[str, Any]:
    """Confirm and execute the import.

    Parses the bundle, resolves all references, and creates real database
    entities: Schema/SchemaVersion, Agent, Pipeline, PipelineEdge, and a
    LibraryPrimitive for the workflow.
    """
    try:
        bundle = json.loads(req.bundle_json)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid bundle JSON",
        ) from None

    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            result = await materialize_import(
                session,
                org_id=principal.organisation_id,
                created_by=principal.account_id,
                bundle=bundle,
                owner_team_id=req.owner_team_id,
                pipeline_name_override=req.pipeline_name_override,
                model_backend_overrides=req.model_backend_overrides,
                schema_id_overrides=req.schema_overrides,
                schema_version_overrides=req.schema_version_overrides,
                connector_instance_overrides=req.connector_overrides,
            )
    except IntegrityError:
        _log.exception("library.confirm_import_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.warning("confirm_import_endpoint: ProgrammingError — missing DB table or migration", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.warning("confirm_import_endpoint: SQLAlchemyError — database connection failure", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed. Please try again later.",
        ) from None

    return {
        "status": "imported",
        "pipeline_id": result["pipeline_id"],
        "pipeline_name": result["pipeline_name"],
        "primitive_id": result["primitive_id"],
        "agent_count": result["agent_count"],
        "edge_count": result["edge_count"],
        "schema_count": result["schema_count"],
        "warnings": result.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------


@router.get("/{primitive_id}/ratings")
@handle_db_errors("library.list_ratings_endpoint")
async def list_ratings_endpoint(
    primitive_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> RatingListResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            result = await list_ratings_for_primitive(session, primitive_id, page=page, page_size=page_size)
    except IntegrityError:
        _log.exception("library.list_ratings_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.list_ratings_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("list_ratings_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return RatingListResponse(
        items=[RatingResponse.model_validate(r) for r in result.items],
        total=result.total,
    )


@router.get("/{primitive_id}/ratings/aggregate")
@handle_db_errors("library.get_rating_aggregate_endpoint")
async def get_rating_aggregate_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> RatingAggregateResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            avg, count = await get_rating_aggregate(session, primitive_id)
    except IntegrityError:
        _log.exception("library.get_rating_aggregate_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.get_rating_aggregate_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("get_rating_aggregate_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return RatingAggregateResponse(
        average_rating=float(avg) if avg is not None else None,
        review_count=count,
    )


@router.post(
    "/{primitive_id}/ratings",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("library.submit_rating_endpoint")
async def submit_rating_endpoint(
    primitive_id: uuid.UUID,
    req: RatingSubmit,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> RatingResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            rating = await submit_rating(
                session,
                org_id=principal.organisation_id,
                primitive_id=primitive_id,
                thumbs_up=req.thumbs_up,
                comment=req.comment,
                account_id=principal.account_id,
            )
            await update_primitive_ratings_aggregate(session, primitive_id)
    except SelfRatingError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    except DuplicateRatingError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except RatingCooldownError as e:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e)) from e
    except CopyToAdaptError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    except IntegrityError as e:
        _log.exception("library.submit_rating_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from e
    except ProgrammingError:
        _log.exception("library.submit_rating_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("submit_rating_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return RatingResponse.model_validate(rating)


@router.post(
    "/{primitive_id}/ratings/abuse",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("library.submit_abuse_report_endpoint")
async def submit_abuse_report_endpoint(
    primitive_id: uuid.UUID,
    req: AbuseReportSubmit,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> AbuseReportResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            report = await submit_abuse_report(
                session,
                org_id=principal.organisation_id,
                primitive_id=primitive_id,
                rating_id=req.rating_id,
                reporter_account_id=principal.account_id,
                reason=req.reason,
            )
    except IntegrityError:
        _log.exception("library.submit_abuse_report_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.submit_abuse_report_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("submit_abuse_report_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return AbuseReportResponse.model_validate(report)


# ---------------------------------------------------------------------------
# Create pipeline from template
# ---------------------------------------------------------------------------


def _build_pipeline_from_template(
    primitive: Any,
    name_override: str | None,
    description_override: str | None,
) -> tuple[Any, str | None, list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Extract pipeline structure from a library primitive's content_json.

    Returns (name, description, graph_nodes, edges, agent_count, edge_count).
    """
    content = primitive.content_json
    agents = content.get("agents", [])
    graph_nodes = content.get("graph_nodes", [])
    edges = content.get("edges", [])

    name = name_override or getattr(primitive, "name", "Pipeline from Template")
    description = description_override or getattr(primitive, "description", None)

    # Build a map from template string IDs to stable UUIDs so PipelineEdge
    # foreign keys (Uuid columns) don't crash on human-readable IDs.
    node_id_map = _build_template_node_id_map(graph_nodes)

    pipeline_nodes = _convert_template_nodes(graph_nodes, agents, node_id_map)
    pipeline_edges = _convert_template_edges(edges, node_id_map)

    return name, description, pipeline_nodes, pipeline_edges, len(agents), len(edges)


def _build_template_node_id_map(graph_nodes: list[dict[str, Any]]) -> dict[str, str]:
    """Map each template node's human-readable id to a stable UUID string."""
    node_id_map: dict[str, str] = {}
    for node in graph_nodes:
        tid = node.get("id", "")
        if tid:
            node_id_map[tid] = str(uuid.uuid4())
    return node_id_map


def _node_label(
    node: dict[str, Any],
    agents: list[dict[str, Any]],
    agent_index: int | None,
) -> str:
    """Compute the display label for a template node.

    Manual-gate nodes always prefer their own label; otherwise an in-range
    ``agent_index`` falls back to the referenced agent's name.
    """
    if node.get("node_type") == "manual":
        return cast(str, node.get("label", "Manual Gate"))
    if agent_index is not None and agent_index < len(agents):
        return cast(str, node.get("label") or agents[agent_index].get("name", "Agent"))
    return cast(str, node.get("label", "Node"))


def _convert_template_nodes(
    graph_nodes: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    node_id_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert template graph nodes to pipeline graph nodes.

    Template nodes use ``agent_index`` to reference template agents. The
    agent definition is embedded in the node metadata so the frontend can
    resolve it later when the user configures real agents.
    """
    pipeline_nodes: list[dict[str, Any]] = []
    for node in graph_nodes:
        tid = node.get("id", "")
        agent_index = node.get("agent_index")
        pipeline_node: dict[str, Any] = {
            "id": node_id_map.get(tid, tid or str(uuid.uuid4())),
            "node_type": node.get("node_type", "agent"),
            "position": node.get("position", {"x": 0, "y": 0}),
            "label": _node_label(node, agents, agent_index),
        }
        if agent_index is not None and agent_index < len(agents):
            pipeline_node["template_agent"] = agents[agent_index]
        if node.get("node_type") == "manual":
            pipeline_node["output_schema_id"] = node.get("output_schema_id")

        pipeline_nodes.append(pipeline_node)
    return pipeline_nodes


def _convert_template_edges(
    edges: list[dict[str, Any]],
    node_id_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert template edges to pipeline edge format.

    Source/target are mapped through ``node_id_map`` so human-readable
    template IDs become UUIDs.
    """
    pipeline_edges: list[dict[str, Any]] = []
    for edge in edges:
        old_source = edge.get("source", edge.get("source_node_id", ""))
        old_target = edge.get("target", edge.get("target_node_id", ""))
        pipeline_edge = {
            "id": str(uuid.uuid4()),
            "source_node_id": node_id_map.get(old_source, old_source),
            "target_node_id": node_id_map.get(old_target, old_target),
            "edge_type": edge.get("edge_type", "normal"),
        }
        hitl_config = edge.get("hitl_gate_config")
        if hitl_config:
            pipeline_edge["hitl_gate_config"] = hitl_config
        pipeline_edges.append(pipeline_edge)
    return pipeline_edges


def _add_pipeline_edges(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline: Any,
    edges: list[dict[str, Any]],
) -> None:
    """Persist converted template edges as PipelineEdge rows for a new pipeline."""
    for edge_data in edges:
        session.add(
            PipelineEdge(
                id=uuid.uuid4(),
                organisation_id=org_id,
                pipeline_id=pipeline.id,
                source_node_id=uuid.UUID(edge_data["source_node_id"]),
                target_node_id=uuid.UUID(edge_data["target_node_id"]),
                edge_type=edge_data["edge_type"],
                condition_expression=edge_data.get("condition_expression"),
                hitl_gate_config=edge_data.get("hitl_gate_config"),
                source_port=edge_data.get("source_port", "out"),
                target_port=edge_data.get("target_port", "in"),
            )
        )


def _pipeline_from_template_response(
    pipeline: Any,
    primitive_id: uuid.UUID,
    agent_count: int,
    edge_count: int,
) -> PipelineFromTemplateResponse:
    """Build the pipeline-from-template response from a freshly created pipeline."""
    return PipelineFromTemplateResponse(
        id=pipeline.id,
        organisation_id=pipeline.organisation_id,
        name=pipeline.name,
        description=pipeline.description,
        visibility=pipeline.visibility,
        template_source_id=primitive_id,
        agent_count=agent_count,
        edge_count=edge_count,
        ready_to_run=True,
        created_at=pipeline.created_at,
        updated_at=pipeline.updated_at,
    )


@router.post(
    "/{primitive_id}/create-pipeline",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors(_CODE_LIBRARY_CREATE_PIPELINE_TEMPLATE)
async def create_pipeline_from_template_endpoint(
    primitive_id: uuid.UUID,
    req: CreatePipelineFromTemplateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("pipeline.create"),
) -> PipelineFromTemplateResponse:
    try:
        primitive = await get_primitive(session, principal.organisation_id, primitive_id)
    except IntegrityError:
        _log.exception(_CODE_LIBRARY_CREATE_PIPELINE_TEMPLATE)
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception(_CODE_LIBRARY_CREATE_PIPELINE_TEMPLATE)
        raise _not_implemented_error() from None
    if primitive is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )

    if primitive.primitive_type != "pipeline_template":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Primitive type '{primitive.primitive_type}' is not a pipeline_template",
        )

    name, description, graph_nodes, edges, agent_count, edge_count = _build_pipeline_from_template(
        primitive,
        req.name,
        req.description,
    )

    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await create_pipeline(
                session,
                org_id=principal.organisation_id,
                name=name,
                account_id=principal.account_id,
                description=description,
                run_context_defaults={"library_source_id": str(primitive_id), "library_template_name": primitive.name},
            )
            pipeline.graph_nodes_json = graph_nodes
            _add_pipeline_edges(session, principal.organisation_id, pipeline, edges)
            await session.flush()
    except IntegrityError:
        _log.exception(_CODE_LIBRARY_CREATE_PIPELINE_TEMPLATE)
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception(_CODE_LIBRARY_CREATE_PIPELINE_TEMPLATE)
        raise _not_implemented_error() from None

    return _pipeline_from_template_response(pipeline, primitive_id, agent_count, edge_count)


@router.post(
    "/{primitive_id}/create-lifecycle-map",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("library.create_lifecycle_map_from_primitive_endpoint")
async def create_lifecycle_map_from_primitive_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("library.copy"),
) -> LifecycleMapResponse:
    """Copy-to-adapt a ``lifecycle_map`` library primitive into a real map.

    Creates a NEW lifecycle map in the org from the primitive's exported
    content (name collisions are suffixed with "(imported)"), so a shared map
    can be copied and adapted without touching the source.
    """
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            primitive = await get_primitive(session, principal.organisation_id, primitive_id)
            if primitive is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Primitive {primitive_id} not found",
                )
            if primitive.primitive_type != LIFECYCLE_MAP_PRIMITIVE_TYPE:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(f"Primitive type '{primitive.primitive_type}' is not '{LIFECYCLE_MAP_PRIMITIVE_TYPE}'"),
                )
            lifecycle_map = await materialize_map_from_primitive(
                session,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                primitive=primitive,
            )
            await session.refresh(lifecycle_map)
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except (LifecycleMapBundleError, LifecycleMapContentError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except IntegrityError:
        _log.exception("library.create_lifecycle_map_from_primitive_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.create_lifecycle_map_from_primitive_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("create_lifecycle_map_from_primitive_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return LifecycleMapResponse.model_validate(lifecycle_map)


# ---------------------------------------------------------------------------
# Community contribution endpoints
# ---------------------------------------------------------------------------


class CommunityContributeRequest(BaseModel):
    primitive_type: str = Field(
        ...,
        pattern=r"^(schema|workflow|agent|integration|test_fixture|composite|lifecycle_map)$",
    )
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list)
    content_json: dict[str, Any]
    source_url: str | None = None


class CommunityContributionListResponse(BaseModel):
    items: list[LibraryPrimitiveResponse]
    total: int
    page: int
    page_size: int


@router.post("/community/contribute", status_code=status.HTTP_201_CREATED)
@handle_db_errors("library.community_contribute_endpoint")
async def community_contribute_endpoint(
    req: CommunityContributeRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIBRARY_MANAGE),
) -> LibraryPrimitiveResponse:
    """Submit a community library contribution."""
    try:
        org_id = _require_organisation_id(principal)
        result = await contribute_primitive(
            session,
            org_id=org_id,
            created_by=principal.account_id,
            primitive_type=req.primitive_type,
            name=req.name,
            slug=req.slug,
            description=req.description,
            tags=req.tags,
            content_json=req.content_json,
            source_url=req.source_url,
        )
    except IntegrityError:
        _log.exception("library.community_contribute_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.community_contribute_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("community_contribute_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return LibraryPrimitiveResponse.model_validate(result)


@router.get("/community/contributions")
@handle_db_errors("library.list_community_contributions_endpoint")
async def list_community_contributions_endpoint(
    contribution_status: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> CommunityContributionListResponse:
    """List the org's own community contributions, optionally filtered by status."""
    try:
        org_id = _require_organisation_id(principal)
        try:
            result = await list_org_contributions(
                session,
                org_id,
                contribution_status=contribution_status,
                page=page,
                page_size=page_size,
            )
        except IntegrityError:
            _log.exception("library.list_community_contributions_endpoint")
            raise _conflict_error() from None
        except ProgrammingError:
            _log.exception("library.list_community_contributions_endpoint")
            _log.warning(
                "list_community_contributions_endpoint: ProgrammingError — missing DB table or migration",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=MSG_FEATURE_NOT_AVAILABLE,
            ) from None
        items = [LibraryPrimitiveResponse.model_validate(p) for p in result.items]
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_community_contributions_endpoint: unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing contributions.",
        ) from None
    return CommunityContributionListResponse(
        items=items,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/admin/library/community/publish/{primitive_id}",
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("library.admin_publish_contribution_endpoint")
async def admin_publish_contribution_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    _: None = Depends(require_system_admin),
) -> LibraryPrimitiveResponse:
    """Publish a community contribution to the community library (admin only)."""
    try:
        org_id = _require_organisation_id(principal)
        result = await publish_contribution(
            session,
            org_id,
            primitive_id,
        )
    except ContributionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contribution {primitive_id} not found",
        ) from None
    except ContributionInvalidTransitionError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None
    except IntegrityError:
        _log.exception("library.admin_publish_contribution_endpoint")
        raise _conflict_error() from None
    except ProgrammingError:
        _log.exception("library.admin_publish_contribution_endpoint")
        raise _not_implemented_error() from None
    except SQLAlchemyError:
        _log.exception("admin_publish_contribution_endpoint: SQLAlchemyError")
        raise _unavailable_error() from None
    return LibraryPrimitiveResponse.model_validate(result)
