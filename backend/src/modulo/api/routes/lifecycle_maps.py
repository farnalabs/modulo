"""Lifecycle Map CRUD + version REST API."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission, require_permission_any_credential
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.core.lifecycle_map.advancement import advance_journeys, confirm_reported_refs
from modulo.core.lifecycle_map.import_export import (
    LifecycleMapBundleError,
    build_export_envelope,
    import_lifecycle_map_envelope,
)
from modulo.core.lifecycle_map.journeys import (
    get_map_journey,
    list_journey_runs,
    list_map_journeys,
)
from modulo.core.lifecycle_map.self_report import validate_and_normalise_reported_refs
from modulo.core.lifecycle_map.service import (
    create_lifecycle_map,
    delete_lifecycle_map,
    get_lifecycle_map,
    graduate_stage,
    list_lifecycle_maps,
    restore_lifecycle_map,
    save_map_version,
    update_lifecycle_map,
)
from modulo.core.lifecycle_map.validation import (
    LifecycleMapContentError,
    LifecycleMapPipelineConflictError,
)
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_LIFECYCLE_MAPS_AUDIT_FAILED = "lifecycle_maps.audit_failed"
_CODE_LIFECYCLE_MAP_LIST = "lifecycle_map.list"
_MSG_DATABASE_TEMPORARILY_UNAVAILABLE = "Database temporarily unavailable."
_CODE_LIFECYCLE_MAP_CREATE = "lifecycle_map.create"
_CODE_LIFECYCLE_MAPS_CREATE_LIFECYCLE = "lifecycle_maps.create_lifecycle_map_endpoint"
_MSG_LIFECYCLE_MAP_CONFLICTS_EXISTING = "Lifecycle map conflicts with an existing resource."
_CODE_LIFECYCLE_MAPS_IMPORT_LIFECYCLE = "lifecycle_maps.import_lifecycle_map_endpoint"
_MSG_LIFECYCLE_MAP_NOT_FOUND = "Lifecycle map not found"
_CODE_LIFECYCLE_MAP_UPDATE = "lifecycle_map.update"
_CODE_LIFECYCLE_MAPS_UPDATE_LIFECYCLE = "lifecycle_maps.update_lifecycle_map_endpoint"
_CODE_LIFECYCLE_MAPS_RESTORE_LIFECYCLE = "lifecycle_maps.restore_lifecycle_map_endpoint"
_CODE_LIFECYCLE_MAPS_SAVE_VERSION = "lifecycle_maps.save_version_endpoint"
_MSG_MAP_VERSION_CONFLICTS_EXISTING = "Map version conflicts with an existing lifecycle map resource."
_CODE_LIFECYCLE_MAPS_UPDATE_VERSION = "lifecycle_maps.update_version_endpoint"
_CODE_LIFECYCLE_MAPS_GRADUATE_STAGE = "lifecycle_maps.graduate_stage_endpoint"
_CODE_LIFECYCLE_MAPS_LIST_JOURNEYS = "lifecycle_maps.list_journeys_endpoint"
_CODE_LIFECYCLE_MAPS_GET_JOURNEY = "lifecycle_maps.get_journey_endpoint"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/lifecycle-maps", tags=["lifecycle_maps"])


class LifecycleMapCreate(TeamVisibilityMixin):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern=r"^(org|team)$")
    version: int = Field(default=1, ge=1)
    content_json: dict[str, Any] = Field(default_factory=dict[str, Any])


class LifecycleMapUpdate(TeamVisibilityMixin):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = Field(None, pattern=r"^(org|team)$")
    content_json: dict[str, Any] | None = None


class LifecycleMapTransfer(BaseModel):
    """Portable export/import envelope for a lifecycle map (PRD §8.31.9).

    This is the primitive shape ``GET .../export`` returns and ``POST
    /import`` accepts: ``content_json`` holds the canonical stages/edges/notes
    graph and is validated with the same rules as an editor save. ``format_version``
    is ``2`` and the optional ``versions`` array carries the version history
    (each version's stages/edges/notes + metadata); a v1 envelope without
    ``versions`` still imports as a single-version map.
    """

    primitive_type: Literal["lifecycle_map"] = "lifecycle_map"
    format_version: str = "2"
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    content_json: dict[str, Any] = Field(default_factory=dict)
    versions: list[dict[str, Any]] | None = None


class LifecycleMapResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    owner_team_id: uuid.UUID | None
    visibility: str
    version: int
    content_json: dict[str, Any]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class LifecycleMapListResponse(BaseModel):
    items: list[LifecycleMapResponse]
    total: int
    page: int
    page_size: int


class VersionSaveRequest(BaseModel):
    """Stage/edge canvas payload POSTed by the visual editor.

    Stages/edges are opaque dicts so editor fields survive round-trips; the
    shape is validated and canonicalised by ``normalize_content``.
    """

    stages: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)


class GraduateStageRequest(BaseModel):
    pipeline_id: str | None = None


class LifecycleMapStageEditorItem(BaseModel):
    """A journey/map-stage in the editor wire shape."""

    id: str
    name: str
    description: str | None = None
    stage_type: str
    pipeline_id: str | None = None
    external_url: str | None = None
    owner: str | None = None
    graduated: bool = False


class LifecycleMapEdgeEditorItem(BaseModel):
    """A transition edge in the editor wire shape."""

    id: str
    source_stage_id: str
    target_stage_id: str
    trigger_type: str | None = None
    description: str | None = None
    condition_expression: str | None = None
    estimated_frequency: str | None = None
    trigger_link: str | None = None


class LifecycleMapVersionResponse(BaseModel):
    id: uuid.UUID
    lifecycle_map_id: uuid.UUID
    version: int
    version_number: int
    stages: list[LifecycleMapStageEditorItem]
    edges: list[LifecycleMapEdgeEditorItem]
    created_by: str | None = None
    created_at: datetime
    notes: str = ""


class LifecycleMapStageItem(BaseModel):
    """A journey/map-stage in the map-detail wire shape (store/read path)."""

    id: str
    name: str
    description: str | None = None
    type: str
    owner_badge: str | None = None
    graduated: bool = False
    pipeline_id: str | None = None
    external_url: str | None = None


class LifecycleMapTransitionItem(BaseModel):
    id: str
    source_stage_id: str
    target_stage_id: str
    trigger_type: str | None = None
    description: str | None = None


class LifecycleMapVersionMeta(BaseModel):
    version: int
    created_at: datetime
    created_by: str | None = None


class LifecycleMapDetailResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    owner: str | None = None
    owner_team_id: uuid.UUID | None
    visibility: str
    version: int
    current_version: int
    stages: list[LifecycleMapStageItem]
    transitions: list[LifecycleMapTransitionItem]
    versions: list[LifecycleMapVersionMeta]
    content_json: dict[str, Any]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JourneyCurrentStage(BaseModel):
    """The map stage a journey currently sits in (latest stage identity)."""

    map_id: uuid.UUID
    version: int | None = None
    stage_id: str
    stage_name: str | None = None
    position: int | None = None


class JourneySummaryResponse(BaseModel):
    """One journey in the map-scoped list wire shape."""

    kind: str
    ref: str
    canonical_work_item_id: uuid.UUID
    current_stage: JourneyCurrentStage | None = None
    status: str | None = None
    provenance: str | None = None
    run_count: int = 0
    unattributed: bool = False
    latest_run_id: uuid.UUID | None = None
    updated_at: datetime


class JourneyListResponse(BaseModel):
    items: list[JourneySummaryResponse]
    next_cursor: str | None = None


class JourneyRunHistoryItem(BaseModel):
    run_id: uuid.UUID
    status: str | None = None
    completed_at: datetime | None = None
    provenance: str | None = None


class JourneyDetailResponse(JourneySummaryResponse):
    runs: list[JourneyRunHistoryItem] = Field(default_factory=list)


class JourneySelfReportRequest(BaseModel):
    """Workflow-reported work-item refs (advisory self-report).

    ``work_item_refs`` entries are arbitrary JSON (not strictly dicts) so a
    malformed entry (non-dict, missing kind/ref, bad status) is REJECTED and
    counted per-ref by ``validate_and_normalise_reported_refs`` instead of
    failing the whole request with a 422 — fail-open per ref.
    ``pipeline_id`` is the optional Modulo pipeline that completed the stage;
    when it is a stage of this map, the matched journey advances into it.
    ``stage_id`` is the map stage id the workflow completed; used when the
    stage is external (a GitHub Actions workflow, not a Modulo pipeline) and
    has no ``pipeline_id`` — the stage is resolved against this map's current
    ``lifecycle_map_stages`` projection so the journey advances into it
    (e.g. the merge queue reports ``stage_id: "merge"``, the deploy agent
    ``stage_id: "deploy"``).
    """

    work_item_refs: list[Any] = Field(default_factory=list)
    pipeline_id: uuid.UUID | None = None
    stage_id: str | None = Field(None, max_length=255)


class JourneySelfReportResponse(BaseModel):
    """Per-ref outcome summary for one self-report request.

    ``accepted`` refs matched an existing journey and were advanced;
    ``unmatched`` refs were valid but had no journey row (dropped — never
    minted); ``rejected`` refs were malformed or dropped by the 100-entry cap.
    """

    accepted: int
    rejected: int
    unmatched: int


def _content_dict(lm: Any) -> dict[str, Any]:
    content = getattr(lm, "content_json", None)
    return content if isinstance(content, dict) else {}


def _version_actor(lm: Any) -> str | None:
    """Account that produced the current version state.

    v1 has no immutable version history: the map row IS the active version, so
    the version entry's ``created_by`` reflects the account that last saved it
    (``updated_by``), falling back to the original creator (``account_id``) so
    rows created before the actor stamping still read back readable.
    """
    updated_by = getattr(lm, "updated_by", None)
    if updated_by is not None:
        return str(updated_by)
    account_id = getattr(lm, "account_id", None)
    return str(account_id) if account_id is not None else None


async def _record_audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    event_type: str,
    account_id: uuid.UUID,
    resource_id: uuid.UUID,
    payload_json: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit write for a lifecycle-map mutation (fail-open).

    Audit is a side channel: a failure here must never turn a committed map
    mutation into an error response. The event is appended in a savepoint
    nested inside the caller's transaction, so a rollback discards only the
    audit write — never the map mutation or any ORM state the caller still
    reads (an outer rollback would expire them). Audit errors are logged and
    swallowed, following the fail-open route audit convention.
    """
    try:
        async with session.begin_nested():
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=event_type,
                actor_user_id=account_id,
                resource_type="lifecycle_map",
                resource_id=resource_id,
                payload_json=payload_json,
            )
    except ProgrammingError:
        _log.exception(_CODE_LIFECYCLE_MAPS_AUDIT_FAILED)
    except SQLAlchemyError:
        _log.exception(_CODE_LIFECYCLE_MAPS_AUDIT_FAILED)
    except Exception:
        _log.exception(_CODE_LIFECYCLE_MAPS_AUDIT_FAILED)


def _build_version_entry(lm: Any) -> LifecycleMapVersionResponse:
    """Serialize the active map state as a version entry.

    v1 has no immutable version history: the map's current content_json is
    returned as the single active version, keyed by the map id so the editor
    can round-trip through PUT.
    """
    content = _content_dict(lm)
    stages = [
        LifecycleMapStageEditorItem(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description"),
            stage_type=s.get("type", "placeholder"),
            pipeline_id=s.get("pipeline_id"),
            external_url=s.get("external_url"),
            owner=s.get("owner"),
            graduated=bool(s.get("graduated", False)),
        )
        for s in (content.get("stages") or [])
        if isinstance(s, dict)
    ]
    edges = [
        LifecycleMapEdgeEditorItem(
            id=e.get("id", ""),
            source_stage_id=e.get("source", ""),
            target_stage_id=e.get("target", ""),
            trigger_type=e.get("trigger_type"),
            description=e.get("description"),
            condition_expression=e.get("condition_expression"),
            estimated_frequency=e.get("estimated_frequency"),
            trigger_link=e.get("trigger_link"),
        )
        for e in (content.get("edges") or [])
        if isinstance(e, dict)
    ]
    notes = content.get("notes")
    return LifecycleMapVersionResponse(
        id=lm.id,
        lifecycle_map_id=lm.id,
        version=lm.version,
        version_number=lm.version,
        stages=stages,
        edges=edges,
        created_by=_version_actor(lm),
        created_at=lm.updated_at,
        notes=notes if isinstance(notes, str) else "",
    )


def _build_detail(lm: Any) -> LifecycleMapDetailResponse:
    """Serialize the map in the store/read shape (decoded stages + version meta)."""
    content = _content_dict(lm)
    stages = [
        LifecycleMapStageItem(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description"),
            type=s.get("type", "placeholder"),
            owner_badge=s.get("owner"),
            graduated=bool(s.get("graduated", False)),
            pipeline_id=s.get("pipeline_id"),
            external_url=s.get("external_url"),
        )
        for s in (content.get("stages") or [])
        if isinstance(s, dict)
    ]
    transitions = [
        LifecycleMapTransitionItem(
            id=e.get("id", ""),
            source_stage_id=e.get("source", ""),
            target_stage_id=e.get("target", ""),
            trigger_type=e.get("trigger_type"),
            description=e.get("description"),
        )
        for e in (content.get("edges") or [])
        if isinstance(e, dict)
    ]
    return LifecycleMapDetailResponse(
        id=lm.id,
        organisation_id=lm.organisation_id,
        name=lm.name,
        description=lm.description,
        owner=None,
        owner_team_id=lm.owner_team_id,
        visibility=lm.visibility,
        version=lm.version,
        current_version=lm.version,
        stages=stages,
        transitions=transitions,
        versions=[LifecycleMapVersionMeta(version=lm.version, created_at=lm.updated_at, created_by=_version_actor(lm))],
        content_json=content,
        archived_at=lm.archived_at,
        created_at=lm.created_at,
        updated_at=lm.updated_at,
    )


@router.get("")
@handle_db_errors("lifecycle_maps.list_lifecycle_maps_endpoint")
async def list_lifecycle_maps_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    owner_team_id: uuid.UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_LIST),
) -> LifecycleMapListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_lifecycle_maps(
                session,
                page=page,
                page_size=page_size,
                owner_team_id=owner_team_id,
                include_archived=include_archived,
            )
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.list_lifecycle_maps_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.list_lifecycle_maps_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return LifecycleMapListResponse(
        items=[LifecycleMapResponse.model_validate(m) for m in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors(_CODE_LIFECYCLE_MAPS_CREATE_LIFECYCLE)
async def create_lifecycle_map_endpoint(
    req: LifecycleMapCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_CREATE),
) -> LifecycleMapResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await create_lifecycle_map(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
                version=req.version,
                content_json=req.content_json,
            )
            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="lifecycle_map.created",
                account_id=principal.account_id,
                resource_id=lifecycle_map.id,
                payload_json={"name": lifecycle_map.name},
            )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_CREATE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_CREATE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_LIFECYCLE_MAP_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_CREATE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.create")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return LifecycleMapResponse.model_validate(lifecycle_map)


@router.post("/import", status_code=status.HTTP_201_CREATED)
@handle_db_errors(_CODE_LIFECYCLE_MAPS_IMPORT_LIFECYCLE)
async def import_lifecycle_map_endpoint(
    req: LifecycleMapTransfer,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_CREATE),
) -> LifecycleMapResponse:
    """Import an exported lifecycle-map envelope, creating a new map in the org.

    Content is validated with the same rules as an editor save (normalize_content),
    so a malformed graph returns 422. Imported maps are also registered as
    ``lifecycle_map`` library primitives so they can be listed and copied-to-adapt.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await import_lifecycle_map_envelope(
                session,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                envelope=req.model_dump(),
            )
            await session.refresh(lifecycle_map)
            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="lifecycle_map.created",
                account_id=principal.account_id,
                resource_id=lifecycle_map.id,
                payload_json={"name": lifecycle_map.name, "imported": True},
            )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except (LifecycleMapBundleError, LifecycleMapContentError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_IMPORT_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_IMPORT_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_LIFECYCLE_MAP_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_IMPORT_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.import")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return LifecycleMapResponse.model_validate(lifecycle_map)


@router.get("/{lifecycle_map_id}/export")
@handle_db_errors("lifecycle_maps.export_lifecycle_map_endpoint")
async def export_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_LIST),
) -> LifecycleMapTransfer:
    """Export a lifecycle map's active-version content as a portable envelope."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.export_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.export_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.export")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
    envelope = build_export_envelope(lifecycle_map)
    return LifecycleMapTransfer(**envelope)


@router.get("/{lifecycle_map_id}")
@handle_db_errors("lifecycle_maps.get_lifecycle_map_endpoint")
async def get_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_LIST),
) -> LifecycleMapDetailResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.get_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.get_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.get")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
    return _build_detail(lifecycle_map)


@router.put("/{lifecycle_map_id}")
@handle_db_errors(_CODE_LIFECYCLE_MAPS_UPDATE_LIFECYCLE)
async def update_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    req: LifecycleMapUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_UPDATE),
) -> LifecycleMapResponse:
    updates = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await update_lifecycle_map(
                session,
                lifecycle_map_id,
                updates,
                updated_by=principal.account_id,
            )
            if lifecycle_map is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
            await session.refresh(lifecycle_map)
            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="lifecycle_map.updated",
                account_id=principal.account_id,
                resource_id=lifecycle_map.id,
                payload_json={"version": lifecycle_map.version, "content_changed": "content_json" in updates},
            )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_UPDATE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_UPDATE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_LIFECYCLE_MAP_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_UPDATE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.update")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return LifecycleMapResponse.model_validate(lifecycle_map)


@router.delete("/{lifecycle_map_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("lifecycle_maps.delete_lifecycle_map_endpoint")
async def delete_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.delete"),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_lifecycle_map(session, lifecycle_map_id)
            if deleted:
                await _record_audit(
                    session,
                    org_id=principal.organisation_id,
                    event_type="lifecycle_map.deleted",
                    account_id=principal.account_id,
                    resource_id=lifecycle_map_id,
                    payload_json={},
                )
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.delete_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.delete_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.delete")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)


@router.post("/{lifecycle_map_id}/restore")
@handle_db_errors(_CODE_LIFECYCLE_MAPS_RESTORE_LIFECYCLE)
async def restore_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_CREATE),
) -> LifecycleMapResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await restore_lifecycle_map(
                session,
                lifecycle_map_id,
                updated_by=principal.account_id,
            )
            if lifecycle_map is not None:
                await session.refresh(lifecycle_map)
                await _record_audit(
                    session,
                    org_id=principal.organisation_id,
                    event_type="lifecycle_map.restored",
                    account_id=principal.account_id,
                    resource_id=lifecycle_map.id,
                    payload_json={"name": lifecycle_map.name},
                )
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_RESTORE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_RESTORE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lifecycle map cannot be restored: a stage pipeline is already registered in another active map.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_RESTORE_LIFECYCLE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.restore")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found or not deleted")
    return LifecycleMapResponse.model_validate(lifecycle_map)


@router.get("/{lifecycle_map_id}/versions")
@handle_db_errors("lifecycle_maps.list_versions_endpoint")
async def list_lifecycle_map_versions_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_LIST),
) -> list[LifecycleMapVersionResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.list_versions_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.list_versions_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.list_versions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
    return [_build_version_entry(lifecycle_map)]


@router.post(
    "/{lifecycle_map_id}/versions",
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors(_CODE_LIFECYCLE_MAPS_SAVE_VERSION)
async def save_lifecycle_map_version_endpoint(
    lifecycle_map_id: uuid.UUID,
    req: VersionSaveRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_UPDATE),
) -> LifecycleMapVersionResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await save_map_version(
                session,
                lifecycle_map_id,
                stages=req.stages,
                edges=req.edges,
                notes=req.notes,
                updated_by=principal.account_id,
            )
            if lifecycle_map is not None:
                await session.refresh(lifecycle_map)
                await _record_audit(
                    session,
                    org_id=principal.organisation_id,
                    event_type="lifecycle_map.version_saved",
                    account_id=principal.account_id,
                    resource_id=lifecycle_map.id,
                    payload_json={
                        "version": lifecycle_map.version,
                        "stages": len(req.stages),
                        "edges": len(req.edges),
                    },
                )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_SAVE_VERSION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_SAVE_VERSION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_MAP_VERSION_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_SAVE_VERSION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.save_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
    return _build_version_entry(lifecycle_map)


@router.put("/{lifecycle_map_id}/versions/{version_id}")
@handle_db_errors(_CODE_LIFECYCLE_MAPS_UPDATE_VERSION)
async def update_lifecycle_map_version_endpoint(
    lifecycle_map_id: uuid.UUID,
    version_id: uuid.UUID,
    req: VersionSaveRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_UPDATE),
) -> LifecycleMapVersionResponse:
    """Update a version. v1 semantics: the active map state is the only version,
    so this behaves identically to save — ``version_id`` is validated as a UUID
    for contract compatibility but the save targets the map itself.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await save_map_version(
                session,
                lifecycle_map_id,
                stages=req.stages,
                edges=req.edges,
                notes=req.notes,
                updated_by=principal.account_id,
            )
            if lifecycle_map is not None:
                await session.refresh(lifecycle_map)
                await _record_audit(
                    session,
                    org_id=principal.organisation_id,
                    event_type="lifecycle_map.version_saved",
                    account_id=principal.account_id,
                    resource_id=lifecycle_map.id,
                    payload_json={
                        "version": lifecycle_map.version,
                        "stages": len(req.stages),
                        "edges": len(req.edges),
                    },
                )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_UPDATE_VERSION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_UPDATE_VERSION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_MAP_VERSION_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_UPDATE_VERSION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.update_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
    return _build_version_entry(lifecycle_map)


@router.get("/{lifecycle_map_id}/versions/{version}")
@handle_db_errors("lifecycle_maps.get_version_endpoint")
async def get_lifecycle_map_version_endpoint(
    lifecycle_map_id: uuid.UUID,
    version: int = Path(ge=1),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_LIST),
) -> LifecycleMapDetailResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.get_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.get_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.get_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None or lifecycle_map.version != version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map version not found")
    return _build_detail(lifecycle_map)


@router.patch(
    "/{lifecycle_map_id}/versions/{version_id}/stages/{stage_id}/graduate",
)
@handle_db_errors(_CODE_LIFECYCLE_MAPS_GRADUATE_STAGE)
async def graduate_lifecycle_map_stage_endpoint(
    lifecycle_map_id: uuid.UUID,
    version_id: uuid.UUID,
    stage_id: str,
    req: GraduateStageRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_LIFECYCLE_MAP_UPDATE),
) -> LifecycleMapVersionResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await graduate_stage(
                session,
                lifecycle_map_id,
                stage_id=stage_id,
                pipeline_id=req.pipeline_id,
                updated_by=principal.account_id,
            )
            if lifecycle_map is not None:
                await session.refresh(lifecycle_map)
                await _record_audit(
                    session,
                    org_id=principal.organisation_id,
                    event_type="lifecycle_map.stage_graduated",
                    account_id=principal.account_id,
                    resource_id=lifecycle_map.id,
                    payload_json={"stage_id": stage_id, "pipeline_id": req.pipeline_id},
                )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_GRADUATE_STAGE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_GRADUATE_STAGE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_MAP_VERSION_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_GRADUATE_STAGE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.graduate_stage")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
    return _build_version_entry(lifecycle_map)


def _build_journey_current_stage(j: Any) -> JourneyCurrentStage | None:
    """Current map stage from the journey's latest stage identity (null when none)."""
    if j.map_id is None or j.stage_id is None:
        return None
    return JourneyCurrentStage(
        map_id=j.map_id,
        version=j.map_version,
        stage_id=j.stage_id,
        stage_name=j.stage_name,
        position=j.position,
    )


def _build_journey_summary(j: Any, unattributed: bool) -> JourneySummaryResponse:
    return JourneySummaryResponse(
        kind=j.kind,
        ref=j.ref,
        canonical_work_item_id=j.canonical_work_item_id,
        current_stage=_build_journey_current_stage(j),
        status=j.latest_status,
        provenance=j.latest_provenance,
        run_count=j.run_count or 0,
        unattributed=unattributed,
        latest_run_id=j.latest_terminal_run_id,
        updated_at=j.updated_at,
    )


def _team_scope_filter(lifecycle_map: Any) -> uuid.UUID | None:
    """owner_team_id filter for team-scoped maps; None for org-scoped maps."""
    if lifecycle_map.visibility == "team" and lifecycle_map.owner_team_id is not None:
        return cast(uuid.UUID, lifecycle_map.owner_team_id)
    return None


@router.get("/{lifecycle_map_id}/journeys")
@handle_db_errors(_CODE_LIFECYCLE_MAPS_LIST_JOURNEYS)
async def list_journeys_endpoint(
    lifecycle_map_id: uuid.UUID,
    kind: str | None = Query(default=None),
    ref: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("run.list"),
) -> JourneyListResponse:
    """Map-scoped journeys (keyset-paginated), optionally filtered by exact kind/ref."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
            if lifecycle_map is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
            owner_team_id = _team_scope_filter(lifecycle_map)
            journeys, next_cursor = await list_map_journeys(
                session,
                map_id=lifecycle_map_id,
                kind=kind,
                ref=ref,
                owner_team_id=owner_team_id,
                cursor=cursor,
                limit=limit,
            )
    except ValueError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_LIST_JOURNEYS)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid kind, ref, or pagination cursor.",
        ) from exc
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_LIST_JOURNEYS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_LIST_JOURNEYS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.list_journeys")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return JourneyListResponse(
        items=[_build_journey_summary(j, unattributed) for j, unattributed in journeys],
        next_cursor=next_cursor,
    )


@router.get("/{lifecycle_map_id}/journeys/{kind}/{ref}")
@handle_db_errors(_CODE_LIFECYCLE_MAPS_GET_JOURNEY)
async def get_journey_endpoint(
    lifecycle_map_id: uuid.UUID,
    kind: str,
    ref: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("run.list"),
) -> JourneyDetailResponse:
    """Single journey detail incl. recent run history.

    ``kind`` and ``ref`` are path params: refs containing ``/`` must be
    percent-encoded (``%2F``) so they survive URL routing.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
            if lifecycle_map is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
            owner_team_id = _team_scope_filter(lifecycle_map)
            journey_result = await get_map_journey(
                session,
                map_id=lifecycle_map_id,
                kind=kind,
                ref=ref,
                owner_team_id=owner_team_id,
            )
            if journey_result is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Journey not found")
            journey, unattributed = journey_result
            runs = await list_journey_runs(session, journey=journey)
    except ValueError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_GET_JOURNEY)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid kind or ref.",
        ) from exc
    except ProgrammingError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_GET_JOURNEY)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_LIFECYCLE_MAPS_GET_JOURNEY)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.get_journey")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return JourneyDetailResponse(
        **_build_journey_summary(journey, unattributed).model_dump(),
        runs=[
            JourneyRunHistoryItem(
                run_id=r.id,
                status=r.status,
                completed_at=r.completed_at,
                provenance=r.trigger_type,
            )
            for r in runs
        ],
    )


@router.post("/{lifecycle_map_id}/journeys/self-report")
@handle_db_errors("lifecycle_maps.self_report_journeys_endpoint")
async def self_report_journeys_endpoint(
    lifecycle_map_id: uuid.UUID,
    req: JourneySelfReportRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission_any_credential("run.trigger"),
) -> JourneySelfReportResponse:
    """Ingest workflow-reported work-item refs to advance existing journeys.

    Called by external workflows (merge queue, deploy agent) that completed a
    lifecycle-map stage and want the journeys they touched to reflect it.
    Per the FAR-143 spec v6 rule, self-report is ADVISORY: a reported ref can
    only CONFIRM / MATCH an existing journey keyed by the same canonical
    ``(org, kind, ref)`` — a ref with no journey row is dropped (counted as
    unmatched) and is NEVER minted, and no runs are created or touched. Each
    confirmed journey is advanced via ``advance_journeys`` with ``status``
    ``"complete"`` (the workflow reached this endpoint, so its stage
    completed), ``completed_at`` = now and no backing run (the run's
    ``latest_terminal_run_id`` is preserved, not overwritten).

    The request body is already the self-report wire shape, so entries flow
    straight through ``validate_and_normalise_reported_refs`` — the same
    per-entry validation/canonicalisation the run-finalise path applies to
    merged run outputs (``parse_self_report_refs`` is only needed for nested
    run-output trees). A malformed entry is rejected and counted, never a
    whole-request 422 (fail-open per ref).

    When ``stage_id`` is supplied it is resolved against this map's CURRENT
    ``lifecycle_map_stages`` projection (the table's ``(map_id, version,
    stage_id)`` unique key, org-scoped via the RLS context already set) and
    passed to ``advance_journeys`` as the explicit stage. This lets external
    workflows — merge queue (``stage_id: "merge"``), deploy agent
    (``stage_id: "deploy"``) — advance journeys into stages that are external
    (GitHub Actions, no ``pipeline_id``) and therefore unresolvable via
    ``pipeline_id``. An unresolved stage_id (unknown id, or a pipeline-bound
    stage already handled via ``pipeline_id``) simply falls back to the
    pipeline-based path — never an error.

    Auth: the documented CI/CD credential path (PRD §5.2) — a user JWT or an
    org API key (``mk_...``). A GitHub Actions workflow calls this with
    ``Authorization: Bearer mk_<key>`` for a key whose owner holds the
    ``runner`` role. There is no ``run.create`` permission in the registry;
    ``run.trigger`` (runner) is the least-privilege gate that accepts org API
    keys, matching how workflows already trigger runs.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
            if lifecycle_map is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_LIFECYCLE_MAP_NOT_FOUND)
            reported, counters = validate_and_normalise_reported_refs(req.work_item_refs)
            confirmed, unmatched = await confirm_reported_refs(session, principal.organisation_id, reported)
            explicit_stage: LifecycleMapStage | None = None
            if req.stage_id is not None:
                explicit_stage = (
                    await session.execute(
                        select(LifecycleMapStage).where(
                            LifecycleMapStage.map_id == lifecycle_map_id,
                            LifecycleMapStage.stage_id == req.stage_id,
                            LifecycleMapStage.version == lifecycle_map.version,
                        )
                    )
                ).scalar_one_or_none()
            now = datetime.now(UTC)
            advanced = await advance_journeys(
                session,
                principal.organisation_id,
                run_id=None,
                pipeline_id=req.pipeline_id,
                refs=confirmed,
                status="complete",
                completed_at=now,
                run_created_at=now,
                explicit_stage=explicit_stage,
            )
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.self_report_journeys_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.self_report_journeys_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.self_report_journeys")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return JourneySelfReportResponse(
        accepted=advanced,
        rejected=counters["malformed"] + counters["capped"],
        unmatched=unmatched,
    )
