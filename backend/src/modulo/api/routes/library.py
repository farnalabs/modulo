"""Library primitive REST API — browse, export, import, rate."""

import copy
import json
import logging
import traceback
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.library_service import (
    CommunityPrimitiveReadOnlyError,
    copy_to_adapt,
    get_primitive,
    get_primitive_by_slug,
    list_primitives,
)
from modulo.core.workflow_import_export import (
    export_pipeline_bundle,
    extract_bundle_json_from_zip,
    get_existing_agent_names,
    get_existing_pipeline_names,
    materialize_import,
    resolve_connector_type,
    resolve_model_backend,
    resolve_schema,
    suggest_import_name,
)
from modulo.db.crud.library_primitive import (
    create_library_primitive,
    delete_library_primitive,
    update_library_primitive,
)
from modulo.db.crud.pipeline import (
    create_pipeline,
    get_pipeline,
)
from modulo.db.crud.rating import (
    get_rating_aggregate,
    list_ratings_for_primitive,
    submit_abuse_report,
    submit_rating,
    update_primitive_ratings_aggregate,
)
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org, set_rls_user_context

_MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024  # 50 MB

router = APIRouter(prefix="/api/v1/libraries", tags=["libraries"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


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
    download_count: int | None
    average_rating: float | None
    review_count: int | None
    owner_team_id: uuid.UUID | None
    visibility: str
    created_by: uuid.UUID | None = Field(default=None, validation_alias="account_id")
    auto_update: bool = True
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    @classmethod
    def _compute_trust_tier(cls, data: Any) -> Any:
        if data.source == "modulo":
            data.trust_tier = "modulo"
        elif data.source == "registry" and data.verified is True:
            data.trust_tier = "green"
        elif data.source == "registry":
            data.trust_tier = "amber"
        else:
            data.trust_tier = None
        return data


class LibraryPrimitiveListResponse(BaseModel):
    items: list[LibraryPrimitiveResponse]
    total: int
    page: int
    page_size: int
    next_cursor: str | None = None
    has_more: bool = False


class LibraryPrimitiveCreate(BaseModel):
    primitive_type: str = Field(pattern=r"^(schema|workflow|agent|integration|test_fixture|composite)$")
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list)
    content_json: dict[str, Any]
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern=r"^(org|team)$")

    @model_validator(mode="after")
    @classmethod
    def _require_team_id_for_team_visibility(cls, values: Any) -> Any:
        if values.visibility == "team" and values.owner_team_id is None:
            raise ValueError("owner_team_id is required when visibility is 'team'")
        return values


class LibraryPrimitiveUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None
    content_json: dict[str, Any] | None = None
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = Field(default=None, pattern=r"^(org|team)$")
    auto_update: bool | None = None

    @model_validator(mode="after")
    @classmethod
    def _require_team_id_for_team_visibility(cls, values: Any) -> Any:
        if values.visibility == "team" and values.owner_team_id is None:
            raise ValueError("owner_team_id is required when visibility is 'team'")
        return values


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


@router.get("", response_model=LibraryPrimitiveListResponse)
async def list_library_primitives_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    primitive_type: str | None = None,
    search: str | None = None,
    source: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> LibraryPrimitiveListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            include_community = source != "local"
            result = await list_primitives(
                session,
                principal.organisation_id,
                primitive_type=primitive_type,
                search=search,
                page=page,
                page_size=page_size,
                include_community=include_community,
                cursor=cursor,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    try:
        items = [LibraryPrimitiveResponse.model_validate(p) for p in result.items]
    except Exception:
        _log.exception("LibraryPrimitiveResponse.model_validate failed on %d items", len(result.items))
        if result.items:
            _log.error("first item type=%s id=%s", type(result.items[0]).__name__, getattr(result.items[0], "id", "?"))
        raise
    return LibraryPrimitiveListResponse(
        items=items,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.get("/{primitive_id}", response_model=LibraryPrimitiveResponse)
async def get_library_primitive_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> LibraryPrimitiveResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            primitive = await get_primitive(session, principal.organisation_id, primitive_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if primitive is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )
    return LibraryPrimitiveResponse.model_validate(primitive)


# ---------------------------------------------------------------------------
# Create / Update / Delete
# ---------------------------------------------------------------------------


@router.post("", response_model=LibraryPrimitiveResponse, status_code=status.HTTP_201_CREATED)
async def create_library_primitive_endpoint(
    body: LibraryPrimitiveCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> LibraryPrimitiveResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            existing = await get_primitive_by_slug(
                session, principal.organisation_id, body.primitive_type, body.slug
            )
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Primitive with type '{body.primitive_type}' and slug '{body.slug}' already exists",
                )
            prim = await create_library_primitive(
                session,
                org_id=principal.organisation_id,
                source="local",
                primitive_type=body.primitive_type,
                name=body.name,
                slug=body.slug,
                description=body.description,
                author=principal.account_id.hex,
                version="1.0",
                tags=body.tags,
                content_json=body.content_json,
                source_url=None,
                forked_from=None,
                checksum=None,
                ed25519_signature=None,
                verified=None,
                download_count=None,
                average_rating=None,
                review_count=None,
                owner_team_id=body.owner_team_id,
                visibility=body.visibility,
                account_id=principal.account_id,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    return LibraryPrimitiveResponse.model_validate(prim)


@router.patch("/{primitive_id}", response_model=LibraryPrimitiveResponse)
async def update_library_primitive_endpoint(
    primitive_id: uuid.UUID,
    body: LibraryPrimitiveUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> LibraryPrimitiveResponse:
    updates = body.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            prim = await update_library_primitive(session, primitive_id, updates)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if prim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )
    return LibraryPrimitiveResponse.model_validate(prim)


@router.delete("/{primitive_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library_primitive_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_library_primitive(session, primitive_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )


# ---------------------------------------------------------------------------
# Copy-to-adapt
# ---------------------------------------------------------------------------


@router.post("/{primitive_id}/adapt", response_model=LibraryPrimitiveResponse)
async def copy_to_adapt_endpoint(
    primitive_id: uuid.UUID,
    body: CopyToAdaptRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> LibraryPrimitiveResponse:
    try:
        result = await copy_to_adapt(
            session,
            principal.organisation_id,
            primitive_id,
            target_team_id=body.target_team_id,
            account_id=principal.account_id,
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
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    return LibraryPrimitiveResponse.model_validate(result)


# ---------------------------------------------------------------------------
# Workflow export
# ---------------------------------------------------------------------------


@router.post("/export/{pipeline_id}")
async def export_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            pipeline = await get_pipeline(session, pipeline_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {pipeline_id} not found",
        )
    try:
        async with session.begin():
            bundle_bytes = await export_pipeline_bundle(session, pipeline_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in pipeline.name)
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
    principal: AuthenticatedPrincipal,
    bundle: dict[str, Any],
) -> ImportBundleResponse:
    """Shared analysis logic — validates a bundle and returns resolution state."""
    bundle = copy.deepcopy(bundle)  # avoid mutating caller's dict
    warnings: list[str] = []
    resolved_schemas: list[dict[str, Any]] = []
    resolved_connectors: list[dict[str, Any]] = []
    resolved_model_backends_list: list[dict[str, Any]] = []
    name_conflicts: list[dict[str, str]] = []

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            pipeline_info = bundle.get("pipeline", {})
            pipeline_name = pipeline_info.get("name", "Unnamed Pipeline")

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

            for schema in bundle.get("schemas", []):
                result = await resolve_schema(session, principal.organisation_id, schema)
                resolved_schemas.append(result)
                if result.get("schema_id"):
                    schema["_resolved_id"] = result["schema_id"]
                    schema["_resolved_version"] = result["version"]
                if result.get("warning"):
                    warnings.append(result["warning"])

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

            for mb in bundle.get("model_backends", []):
                result = await resolve_model_backend(session, principal.organisation_id, mb)
                resolved_model_backends_list.append(result)
                if result.get("model_backend_id"):
                    mb["_resolved_model_backend_id"] = result["model_backend_id"]
                if result.get("warning"):
                    warnings.append(result["warning"])

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

            for node in pipeline_info.get("graph_nodes_json", []):
                binding = node.get("connector_binding", {})
                if isinstance(binding, dict):
                    ctid = binding.get("connector_type_id", "")
                    if ctid and ctid in connector_instance_map:
                        binding["instance_id"] = connector_instance_map[ctid]

            mb_id_by_name: dict[str, str] = {}
            for mb in bundle.get("model_backends", []):
                rid = mb.get("_resolved_model_backend_id")
                if rid:
                    mb_id_by_name[mb.get("name", "")] = rid
            for agent in bundle.get("agents", []):
                mb_name = agent.get("model_backend_name", "")
                if mb_name and mb_name in mb_id_by_name:
                    agent["model_backend_id"] = mb_id_by_name[mb_name]

            teams_result = await session.execute(select(Team).where(Team.organisation_id == principal.organisation_id))
            teams = list(teams_result.scalars())
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    available_teams = [{"id": str(t.id), "name": t.name} for t in teams]

    return ImportBundleResponse(
        warnings=warnings,
        pipeline_name=pipeline_name,
        bundle_json=json.dumps(bundle, default=str),
        resolved_schemas=resolved_schemas,
        resolved_connectors=resolved_connectors,
        resolved_model_backends=resolved_model_backends_list,
        name_conflicts=name_conflicts,
        available_teams=available_teams,
    )


@router.post("/import/upload-zip", response_model=ImportBundleResponse)
async def upload_zip_and_analyse_endpoint(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ImportBundleResponse:
    """Upload a .modulo.zip file, extract bundle.json, and return analysis.

    Replaces the client-side ZIP parsing for a reliable server-side extraction.
    """
    name = file.filename or ""
    if not name.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .zip or .modulo.zip files are accepted",
        )
    if file.size and file.size > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload size exceeds maximum of {_MAX_UPLOAD_SIZE // (1024 * 1024)} MB",
        )
    zip_bytes = await file.read()
    if len(zip_bytes) > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload size exceeds maximum of {_MAX_UPLOAD_SIZE // (1024 * 1024)} MB",
        )
    try:
        bundle = extract_bundle_json_from_zip(zip_bytes)
    except (LookupError, json.JSONDecodeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None

    return await _analyse_bundle(session, principal, bundle)


@router.post("/import/analyse", response_model=ImportBundleResponse)
async def analyse_import_bundle_endpoint(
    body: AnalyseBundleRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ImportBundleResponse:
    """Analyse a bundle JSON and return resolution warnings + available teams.

    Accepts raw JSON body with bundle content, or use /import/upload-zip
    to upload a .modulo.zip file directly.
    """
    return await _analyse_bundle(session, principal, body.bundle)


# ---------------------------------------------------------------------------
# Workflow import — step 2: confirm and materialize
# ---------------------------------------------------------------------------


@router.post("/import/confirm", response_model=dict[str, Any])
async def confirm_import_endpoint(
    body: ImportConfirmRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Confirm and execute the import.

    Parses the bundle, resolves all references, and creates real database
    entities: Schema/SchemaVersion, Agent, Pipeline, PipelineEdge, and a
    LibraryPrimitive for the workflow.
    """
    try:
        bundle = json.loads(body.bundle_json)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid bundle JSON",
        ) from None

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await materialize_import(
                session,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                bundle=bundle,
                owner_team_id=body.owner_team_id,
                pipeline_name_override=body.pipeline_name_override,
                model_backend_overrides=body.model_backend_overrides,
                schema_id_overrides=body.schema_overrides,
                schema_version_overrides=body.schema_version_overrides,
                connector_instance_overrides=body.connector_overrides,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
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


@router.get("/{primitive_id}/ratings", response_model=RatingListResponse)
async def list_ratings_endpoint(
    primitive_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RatingListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_ratings_for_primitive(session, primitive_id, page=page, page_size=page_size)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    return RatingListResponse(
        items=[RatingResponse.model_validate(r) for r in result.items],
        total=result.total,
    )


@router.get("/{primitive_id}/ratings/aggregate", response_model=RatingAggregateResponse)
async def get_rating_aggregate_endpoint(
    primitive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RatingAggregateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            avg, count = await get_rating_aggregate(session, primitive_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    return RatingAggregateResponse(
        average_rating=float(avg) if avg is not None else None,
        review_count=count,
    )


@router.post(
    "/{primitive_id}/ratings",
    response_model=RatingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_rating_endpoint(
    primitive_id: uuid.UUID,
    body: RatingSubmit,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RatingResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            rating = await submit_rating(
                session,
                org_id=principal.organisation_id,
                primitive_id=primitive_id,
                thumbs_up=body.thumbs_up,
                comment=body.comment,
                user_id=principal.account_id,
            )
            await update_primitive_ratings_aggregate(session, primitive_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    return RatingResponse.model_validate(rating)


@router.post(
    "/{primitive_id}/ratings/abuse",
    response_model=AbuseReportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_abuse_report_endpoint(
    primitive_id: uuid.UUID,
    body: AbuseReportSubmit,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> AbuseReportResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            report = await submit_abuse_report(
                session,
                org_id=principal.organisation_id,
                primitive_id=primitive_id,
                rating_id=body.rating_id,
                reporter_user_id=principal.account_id,
                reason=body.reason,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
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
    node_id_map: dict[str, str] = {}
    for node in graph_nodes:
        tid = node.get("id", "")
        if tid:
            node_id_map[tid] = str(uuid.uuid4())

    # Convert template graph nodes to pipeline graph nodes.
    # Template nodes use agent_index to reference template agents.
    # We embed the agent definition in the node metadata so the frontend
    # can resolve it later when the user configures real agents.
    pipeline_nodes: list[dict[str, Any]] = []
    for node in graph_nodes:
        tid = node.get("id", "")
        pipeline_node: dict[str, Any] = {
            "id": node_id_map.get(tid, tid or str(uuid.uuid4())),
            "node_type": node.get("node_type", "agent"),
            "position": node.get("position", {"x": 0, "y": 0}),
        }
        agent_index = node.get("agent_index")
        if agent_index is not None and agent_index < len(agents):
            pipeline_node["template_agent"] = agents[agent_index]
            pipeline_node["label"] = node.get("label") or agents[agent_index].get("name", "Agent")
        else:
            pipeline_node["label"] = node.get("label", "Node")

        if node.get("node_type") == "manual":
            pipeline_node["output_schema_id"] = node.get("output_schema_id")
            pipeline_node["label"] = node.get("label", "Manual Gate")

        pipeline_nodes.append(pipeline_node)

    # Convert template edges to pipeline edge format, mapping source/target
    # through node_id_map so human-readable template IDs become UUIDs.
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

    return name, description, pipeline_nodes, pipeline_edges, len(agents), len(edges)


@router.post(
    "/{primitive_id}/create-pipeline",
    response_model=PipelineFromTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pipeline_from_template_endpoint(
    primitive_id: uuid.UUID,
    body: CreatePipelineFromTemplateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PipelineFromTemplateResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            primitive = await get_primitive(session, principal.organisation_id, primitive_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if primitive is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Primitive {primitive_id} not found",
        )
    if primitive.primitive_type != "pipeline_template":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Primitive type must be 'pipeline_template', got '{primitive.primitive_type}'",
        )

    name, description, graph_nodes, edges, agent_count, edge_count = _build_pipeline_from_template(
        primitive,
        body.name,
        body.description,
    )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            pipeline = await create_pipeline(
                session,
                org_id=principal.organisation_id,
                name=name,
                account_id=principal.account_id,
                description=description,
                run_context_defaults={
                    "library_source_id": str(primitive_id),
                    "library_template_name": primitive.name,
                },
            )

            # Set graph nodes on the pipeline
            pipeline.graph_nodes_json = graph_nodes

            # Create PipelineEdge records
            for edge_data in edges:
                edge_id = edge_data.get("id", "")
                edge = PipelineEdge(
                    id=uuid.UUID(edge_id) if isinstance(edge_id, str) and edge_id else uuid.uuid4(),
                    organisation_id=principal.organisation_id,
                    pipeline_id=pipeline.id,
                    source_node_id=uuid.UUID(edge_data["source_node_id"]) if isinstance(edge_data["source_node_id"], str) else edge_data["source_node_id"],
                    target_node_id=uuid.UUID(edge_data["target_node_id"]) if isinstance(edge_data["target_node_id"], str) else edge_data["target_node_id"],
                    edge_type=edge_data["edge_type"],
                    hitl_gate_config=edge_data.get("hitl_gate_config"),
                )
                session.add(edge)

            await session.flush()
    except (ProgrammingError, DBAPIError):
        _log.exception("create_pipeline_from_template — DB error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except Exception:
        _log.exception("create_pipeline_from_template — unexpected error")
        raise

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


# ---------------------------------------------------------------------------
# Copy-to-adapt via MCP (handled in mcp_server.py, but exposed here for browser)
# ---------------------------------------------------------------------------
