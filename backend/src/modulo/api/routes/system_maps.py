"""System Maps REST API (FAR-2236).

Internal preview surface — gated by the `system_maps` feature flag, dev mode,
operator/admin role, and the `system_maps_access` grant.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_DATABASE_TEMPORARILY_UNAVAILABLE, MSG_FEATURE_NOT_AVAILABLE, MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import (
    _require_internal_system_maps_access,
    get_db_session,
    get_or_create_engine,
    get_or_create_session_factory,
    require_feature,
    require_permission,
)
from modulo.api.schemas.system_maps import (
    AttributionBindingCreate,
    AttributionBindingResponse,
    PublishResponse,
    PublishValidationError,
    PublishValidationErrorHTTP,
    SystemMapCreate,
    SystemMapDiscoveryReport,
    SystemMapDiscoveryRequest,
    SystemMapDiscoverySkipped,
    SystemMapDiscoverySource,
    SystemMapListResponse,
    SystemMapLoopConfigResponse,
    SystemMapNodeResponse,
    SystemMapOverviewBottleneck,
    SystemMapOverviewLoopConfig,
    SystemMapOverviewNodeHealth,
    SystemMapOverviewResponse,
    SystemMapRelationshipResponse,
    SystemMapResponse,
    SystemMapRevisionCreate,
    SystemMapRevisionDetailResponse,
    SystemMapRevisionDiffRequest,
    SystemMapRevisionDiffResponse,
    SystemMapRevisionResponse,
    SystemMapRevisionRestoreAsNewRequest,
    SystemMapRevisionRestoreAsNewResponse,
    SystemMapUpdate,
)
from modulo.api.team_scope import validate_owner_team_for_create
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import SYSTEM_MAPS_ACCESS_PERMISSION
from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event
from modulo.core.system_map_persistence import SystemMapDiscoveryRepository, SystemMapNameConflictError
from modulo.core.system_maps_discovery import (
    DiscoverySource,
    discover_draft,
)
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_folder import PipelineFolder
from modulo.db.models.system_map import SystemMap
from modulo.db.models.trigger import Trigger
from modulo.db.repositories.system_map import SystemMapRepository
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings

_CODE_SYSTEM_MAPS_AUDIT_FAILED = "system_maps.audit_failed"
_CODE_SYSTEM_MAP_LIST = "system_map.list"
_CODE_SYSTEM_MAP_CREATE = "system_map.create"
_CODE_SYSTEM_MAP_UPDATE = "system_map.update"
_CODE_SYSTEM_MAP_DELETE = "system_map.delete"
_CODE_SYSTEM_MAP_REVISION_CREATE = "system_map.revision.create"
_CODE_SYSTEM_MAP_REVISION_GET = "system_map.revision.get"
_CODE_SYSTEM_MAP_REVISION_PUBLISH = "system_map.revision.publish"
_CODE_SYSTEM_MAP_REVISION_ARCHIVE = "system_map.revision.archive"
_CODE_SYSTEM_MAP_REVISION_DELETE = "system_map.revision.delete"
_CODE_SYSTEM_MAP_REVISION_DIFF = "system_map.revision.diff"
_CODE_SYSTEM_MAP_REVISION_RESTORE = "system_map.revision.restore_as_new"
_CODE_SYSTEM_MAP_OVERVIEW = "system_map.overview"
_CODE_SYSTEM_MAP_NODE_CREATE = "system_map.node.create"
_CODE_SYSTEM_MAP_RELATIONSHIP_CREATE = "system_map.relationship.create"
_CODE_SYSTEM_MAP_LOOP_CONFIG_CREATE = "system_map.loop_config.create"
_CODE_SYSTEM_MAP_BINDING_CREATE = "system_map.binding.create"
_CODE_SYSTEM_MAP_BINDING_DELETE = "system_map.binding.delete"

# PublishValidationErrorHTTP is defined in schemas/system_maps.py to avoid
# circular imports (exception_handlers.py imports it, and routes imports
# exception_handlers indirectly via main.py). The exception uses status_code=422
# for internal routing clarity, but the registered exception handler converts it
# to a 200 PublishResponse (wire-contract preservation: clients expect 200 with
# errors in the body, not a 422 error response).

_MSG_SYSTEM_MAP_NOT_FOUND = "System map not found"
_MSG_REVISION_NOT_FOUND = "Revision not found"
_MSG_RELATIONSHIP_NOT_FOUND = "Relationship not found"
_MSG_LOOP_CONFIG_NOT_FOUND = "Loop config not found"
_MSG_BINDING_NOT_FOUND = "Attribution binding not found"
_MSG_MAP_CONFLICTS_EXISTING = "System map conflicts with an existing resource"
_MSG_REVISION_CONFLICTS_EXISTING = "Revision conflicts with an existing resource"

# Endpoint names reused by the logging/error-handling decorators.
_EP_LIST_SYSTEM_MAPS = "system_maps.list_system_maps_endpoint"
_EP_CREATE_SYSTEM_MAP = "system_maps.create_system_map_endpoint"
_EP_GET_SYSTEM_MAP = "system_maps.get_system_map_endpoint"
_EP_UPDATE_SYSTEM_MAP = "system_maps.update_system_map_endpoint"
_EP_DELETE_SYSTEM_MAP = "system_maps.delete_system_map_endpoint"
_EP_GET_SYSTEM_MAP_REVISION = "system_maps.get_system_map_revision_endpoint"
_EP_CREATE_SYSTEM_MAP_REVISION = "system_maps.create_system_map_revision_endpoint"
_EP_PUBLISH_SYSTEM_MAP_REVISION = "system_maps.publish_system_map_revision_endpoint"
_EP_ARCHIVE_SYSTEM_MAP_REVISION = "system_maps.archive_system_map_revision_endpoint"
_EP_DELETE_SYSTEM_MAP_REVISION = "system_maps.delete_system_map_revision_endpoint"
_EP_DIFF_SYSTEM_MAP_REVISION = "system_maps.diff_system_map_revision_endpoint"
_EP_RESTORE_SYSTEM_MAP_REVISION = "system_maps.restore_system_map_revision_endpoint"
_EP_GET_SYSTEM_MAP_OVERVIEW = "system_maps.get_system_map_overview_endpoint"
_EP_CREATE_ATTRIBUTION_BINDING = "system_maps.create_attribution_binding_endpoint"
_EP_LIST_ATTRIBUTION_BINDINGS = "system_maps.list_attribution_bindings_endpoint"
_EP_DELETE_ATTRIBUTION_BINDING = "system_maps.delete_attribution_binding_endpoint"

_DESC_SYSTEM_MAP_ID = "System map ID"
_DESC_REVISION_ID = "Revision ID"

_INTERNAL_SYSTEM_MAP_PERMISSION = SYSTEM_MAPS_ACCESS_PERMISSION
_require_system_maps_feature = require_feature("system_maps")

_log = logging.getLogger(__name__)


async def _get_repo() -> SystemMapRepository:
    """Get a SystemMapRepository instance with the shared session factory."""

    settings = get_settings()
    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async def session_generator() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    return SystemMapRepository(session_factory=session_generator)


router = APIRouter(
    prefix="/api/v1/system-maps",
    tags=["system_maps"],
    dependencies=[Depends(_require_internal_system_maps_access), _require_system_maps_feature],
)


async def _record_audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    event_type: str,
    account_id: uuid.UUID,
    resource_id: uuid.UUID,
    payload_json: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit write for a system-map mutation (fail-open)."""
    try:
        async with session.begin_nested():
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=event_type,
                actor_user_id=account_id,
                resource_type="system_map",
                resource_id=resource_id,
                payload_json=payload_json,
            )
    except Exception:
        _log.exception(_CODE_SYSTEM_MAPS_AUDIT_FAILED)


async def get_repo() -> SystemMapRepository:
    """Dependency to get a SystemMapRepository instance."""
    return await _get_repo()


# ─── SystemMap CRUD ───


@router.get("")
@handle_db_errors(_EP_LIST_SYSTEM_MAPS)
async def list_system_maps_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    name: str | None = Query(default=None),
    owner_team_id: uuid.UUID | None = Query(default=None),
    visibility: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_LIST),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapListResponse:
    """List published maps for org (pagination, filtering by name/owner_team_id)."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            offset = (page - 1) * page_size
            maps = await repository.list_maps_by_org(
                session,
                organisation_id=principal.organisation_id,
                visibility=visibility,
                owner_team_id=owner_team_id,
                name=name,
                limit=page_size,
                offset=offset,
            )

            # Count total for pagination
            from sqlalchemy import func

            count_stmt = (
                select(func.count())
                .select_from(SystemMap)
                .where(SystemMap.organisation_id == principal.organisation_id)
            )
            count_stmt = count_stmt.where(SystemMap.deleted_at.is_(None))
            if visibility:
                count_stmt = count_stmt.where(SystemMap.visibility == visibility)
            if owner_team_id:
                count_stmt = count_stmt.where(SystemMap.owner_team_id == owner_team_id)
            if name:
                count_stmt = count_stmt.where(SystemMap.name.ilike(f"%{name}%"))
            total_result = await session.execute(count_stmt)
            total = total_result.scalar_one()

    except ProgrammingError as exc:
        _log.exception(_EP_LIST_SYSTEM_MAPS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_LIST_SYSTEM_MAPS)
        _log.exception(_EP_LIST_SYSTEM_MAPS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SystemMapListResponse(
        items=[SystemMapResponse.model_validate(m) for m in maps],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(audited("system_map_created", "system_map", principal_dep=get_current_tenant_user_or_api_key))
    ],
)
@handle_db_errors(_EP_CREATE_SYSTEM_MAP)
async def create_system_map_endpoint(
    req: SystemMapCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_CREATE),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapResponse:
    """Create draft map (name, description, owner_team_id, visibility)."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await validate_owner_team_for_create(session, principal, req.owner_team_id)

            system_map = await repository.create_map(
                session,
                organisation_id=principal.organisation_id,
                name=req.name,
                description=req.description,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
                default_map=req.default_map,
                account_id=principal.account_id,
            )
            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.created",
                account_id=principal.account_id,
                resource_id=system_map.id,
                payload_json={"name": system_map.name, "visibility": system_map.visibility},
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception(_EP_CREATE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_EP_CREATE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_MAP_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_CREATE_SYSTEM_MAP)
        _log.exception(_EP_CREATE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.create")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SystemMapResponse.model_validate(system_map)


@router.get("/{map_id}")
@handle_db_errors(_EP_GET_SYSTEM_MAP)
async def get_system_map_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_LIST),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapResponse:
    """Get map with latest published revision summary."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
    except ProgrammingError as exc:
        _log.exception(_EP_GET_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_GET_SYSTEM_MAP)
        _log.exception(_EP_GET_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.get")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    if system_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

    return SystemMapResponse.model_validate(system_map)


@router.put(
    "/{map_id}",
    dependencies=[
        Depends(audited("system_map_updated", "system_map", principal_dep=get_current_tenant_user_or_api_key))
    ],
)
@handle_db_errors(_EP_UPDATE_SYSTEM_MAP)
async def update_system_map_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    req: SystemMapUpdate = Body(...),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_UPDATE),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapResponse:
    """Update a system map."""
    updates = req.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Validate team transition if visibility or owner_team_id changes
            if "visibility" in updates or "owner_team_id" in updates:
                current = await repository.get_map_by_id(
                    session, organisation_id=principal.organisation_id, map_id=map_id
                )
                if current is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)
                from modulo.api.team_scope import validate_team_transition_for_update

                await validate_team_transition_for_update(
                    session,
                    principal,
                    current_owner_team_id=current.owner_team_id,
                    current_visibility=current.visibility,
                    new_owner_team_id=updates.get("owner_team_id", current.owner_team_id),
                    new_visibility=updates.get("visibility", current.visibility),
                )

            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            for key, value in updates.items():
                setattr(system_map, key, value)
            system_map.updated_by = principal.account_id
            await session.flush()

            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.updated",
                account_id=principal.account_id,
                resource_id=system_map.id,
                payload_json=updates,
            )
    except ProgrammingError as exc:
        _log.exception(_EP_UPDATE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_EP_UPDATE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_MAP_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_UPDATE_SYSTEM_MAP)
        _log.exception(_EP_UPDATE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.update")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SystemMapResponse.model_validate(system_map)


@router.delete(
    "/{map_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            audited(
                "system_map_deleted", "system_map", principal_dep=get_current_tenant_user_or_api_key, fail_closed=True
            )
        )
    ],
)
@handle_db_errors(_EP_DELETE_SYSTEM_MAP)
async def delete_system_map_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_DELETE),
    repository: SystemMapRepository = Depends(get_repo),
) -> None:
    """Soft delete map."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await repository.soft_delete_map(
                session,
                organisation_id=principal.organisation_id,
                map_id=map_id,
                deleted_by=principal.account_id,
            )
            if deleted:
                await _record_audit(
                    session,
                    org_id=principal.organisation_id,
                    event_type="system_map.deleted",
                    account_id=principal.account_id,
                    resource_id=map_id,
                    payload_json={},
                )
    except ProgrammingError as exc:
        _log.exception(_EP_DELETE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_DELETE_SYSTEM_MAP)
        _log.exception(_EP_DELETE_SYSTEM_MAP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.delete")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)


# ─── SystemMapRevision CRUD ───


@router.get("/{map_id}/revisions/{rev_id}")
@handle_db_errors(_EP_GET_SYSTEM_MAP_REVISION)
async def get_system_map_revision_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    rev_id: uuid.UUID = Path(..., description=_DESC_REVISION_ID),
    include_graph: bool = Query(default=False, description="Include full graph (nodes, relationships, loop_configs)"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_REVISION_GET),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapRevisionResponse | SystemMapRevisionDetailResponse:
    """Get specific revision."""
    graph: dict[str, Any] | None = None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            revision = await repository.get_revision_by_id(
                session, organisation_id=principal.organisation_id, revision_id=rev_id
            )
            if include_graph:
                graph = await repository.get_full_revision_graph(
                    session, organisation_id=principal.organisation_id, revision_id=rev_id
                )
    except ProgrammingError as exc:
        _log.exception(_EP_GET_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_GET_SYSTEM_MAP_REVISION)
        _log.exception(_EP_GET_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.get_revision")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    if revision is None or revision.system_map_id != map_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

    if include_graph:
        if not graph:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)
        return SystemMapRevisionDetailResponse(
            revision=SystemMapRevisionResponse.model_validate(graph["revision"]),
            nodes=[SystemMapNodeResponse.model_validate(n) for n in graph["nodes"]],
            relationships=[SystemMapRelationshipResponse.model_validate(r) for r in graph["relationships"]],
            loop_configs=[SystemMapLoopConfigResponse.model_validate(lc) for lc in graph["loop_configs"]],
        )

    return SystemMapRevisionResponse.model_validate(revision)


@router.post(
    "/{map_id}/revisions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(
            audited(
                "system_map_revision_created", "system_map_revision", principal_dep=get_current_tenant_user_or_api_key
            )
        )
    ],
)
@handle_db_errors(_EP_CREATE_SYSTEM_MAP_REVISION)
async def create_system_map_revision_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    req: SystemMapRevisionCreate = Body(...),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_REVISION_CREATE),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapRevisionResponse:
    """Create draft revision (nodes, relationships, loop configs)."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            binding_errors = await repository.validate_bindings(
                session,
                organisation_id=principal.organisation_id,
                account_id=principal.account_id,
                org_role=principal.org_role,
                node_bindings=[
                    {
                        "pipeline_id": node.pipeline_id,
                        "connector_instance_id": node.connector_instance_id,
                        "owner_team_id": node.owner_team_id,
                        "eval_id": node.eval_id,
                    }
                    for node in req.nodes
                ],
                action_pipeline_ids=[loop.action_pipeline_id for loop in req.loop_configs],
            )
            if binding_errors:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"message": "Invalid System Map binding", "errors": binding_errors},
                )

            # Get next revision number
            existing_revisions = await repository.list_revisions(
                session,
                organisation_id=principal.organisation_id,
                system_map_id=map_id,
                include_deleted=True,
            )
            next_revision_number = max((r.revision_number for r in existing_revisions), default=0) + 1

            # Create revision
            revision = await repository.create_revision(
                session,
                organisation_id=principal.organisation_id,
                system_map_id=map_id,
                revision_number=next_revision_number,
                status="draft",
                content_json={
                    "nodes": [n.model_dump(mode="json") for n in req.nodes],
                    "relationships": [r.model_dump(mode="json") for r in req.relationships],
                    "loop_configs": [lc.model_dump(mode="json") for lc in req.loop_configs],
                    "notes": req.notes,
                },
            )

            # Create nodes
            for node_req in req.nodes:
                await repository.create_node(
                    session,
                    organisation_id=principal.organisation_id,
                    revision_id=revision.id,
                    node_key=node_req.node_key,
                    node_type=node_req.node_type,
                    display_name=node_req.display_name,
                    config_json=node_req.config_json,
                    pipeline_id=node_req.pipeline_id,
                    connector_instance_id=node_req.connector_instance_id,
                    owner_team_id=node_req.owner_team_id,
                    eval_id=node_req.eval_id,
                    position_x=node_req.position_x,
                    position_y=node_req.position_y,
                )

            # Create relationships
            for rel_req in req.relationships:
                await repository.create_relationship(
                    session,
                    organisation_id=principal.organisation_id,
                    revision_id=revision.id,
                    source_node_key=rel_req.source_node_key,
                    target_node_key=rel_req.target_node_key,
                    relationship_type=rel_req.relationship_type,
                    config_json=rel_req.config_json,
                )

            # Create loop configs
            for loop_req in req.loop_configs:
                await repository.create_loop_config(
                    session,
                    organisation_id=principal.organisation_id,
                    revision_id=revision.id,
                    node_key=loop_req.node_key,
                    target_signal=loop_req.target_signal,
                    observation_window_seconds=loop_req.observation_window_seconds,
                    action_pipeline_id=loop_req.action_pipeline_id,
                    budget_limit_usd=loop_req.budget_limit_usd,
                    cooldown_seconds=loop_req.cooldown_seconds,
                    outcome_metric=loop_req.outcome_metric,
                )

            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.revision.created",
                account_id=principal.account_id,
                resource_id=revision.id,
                payload_json={"system_map_id": str(map_id), "revision_number": revision.revision_number},
            )
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_CREATE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_EP_CREATE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_REVISION_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_CREATE_SYSTEM_MAP_REVISION)
        _log.exception(_EP_CREATE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.create_revision")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SystemMapRevisionResponse.model_validate(revision)


@router.post(
    "/{map_id}/revisions/{rev_id}/publish",
    dependencies=[
        Depends(
            audited(
                "system_map_revision_published", "system_map_revision", principal_dep=get_current_tenant_user_or_api_key
            )
        )
    ],
)
@handle_db_errors(_EP_PUBLISH_SYSTEM_MAP_REVISION)
async def publish_system_map_revision_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    rev_id: uuid.UUID = Path(..., description=_DESC_REVISION_ID),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_REVISION_PUBLISH),
    repository: SystemMapRepository = Depends(get_repo),
) -> PublishResponse:
    """Publish revision (validates acyclicity, loop configs, pipeline bindings)."""
    validation_errors: list[dict[str, Any]] = []

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            # Verify revision exists and belongs to map
            revision = await repository.get_revision_by_id(
                session, organisation_id=principal.organisation_id, revision_id=rev_id
            )
            if not revision or revision.system_map_id != map_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            if revision.status == "published":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revision is already published")

            # Validate before publishing
            is_valid, errors = await repository.validate_publish(
                session,
                organisation_id=principal.organisation_id,
                revision_id=rev_id,
                account_id=principal.account_id,
                org_role=principal.org_role,
            )
            validation_errors = [
                {"code": "VALIDATION_ERROR", "message": err, "node_key": None, "relationship_id": None}
                for err in errors
            ]

            if not is_valid:
                # Raise validation error so audited() records outcome="error"
                # Exception handler will render as 200 PublishResponse with errors
                await session.flush()
                raise PublishValidationErrorHTTP(
                    [PublishValidationError(**e) for e in validation_errors],
                    revision=SystemMapRevisionResponse.model_validate(revision),
                )

            # Publish the revision
            published_revision = await repository.publish_revision(
                session,
                organisation_id=principal.organisation_id,
                system_map_id=map_id,
                revision_id=rev_id,
            )
            if published_revision is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.revision.published",
                account_id=principal.account_id,
                resource_id=published_revision.id,
                payload_json={"system_map_id": str(map_id), "revision_number": published_revision.revision_number},
            )
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_PUBLISH_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_EP_PUBLISH_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_REVISION_CONFLICTS_EXISTING,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_PUBLISH_SYSTEM_MAP_REVISION)
        _log.exception(_EP_PUBLISH_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.publish_revision")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return PublishResponse(
        revision=SystemMapRevisionResponse.model_validate(published_revision),
        validation_errors=[],
    )


@router.post(
    "/{map_id}/revisions/{rev_id}/archive",
    dependencies=[
        Depends(
            audited(
                "system_map_revision_archived", "system_map_revision", principal_dep=get_current_tenant_user_or_api_key
            )
        )
    ],
)
@handle_db_errors(_EP_ARCHIVE_SYSTEM_MAP_REVISION)
async def archive_system_map_revision_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    rev_id: uuid.UUID = Path(..., description=_DESC_REVISION_ID),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_REVISION_ARCHIVE),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapRevisionResponse:
    """Archive revision."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            # Verify revision exists and belongs to map
            revision = await repository.get_revision_by_id(
                session, organisation_id=principal.organisation_id, revision_id=rev_id
            )
            if not revision or revision.system_map_id != map_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            if revision.status == "archived":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revision is already archived")

            # Archive the revision
            revision.status = "archived"
            await session.flush()

            # If this was the published revision, clear the map's published_revision_id
            if system_map.published_revision_id == rev_id:
                system_map.published_revision_id = None
                await session.flush()

            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.revision.archived",
                account_id=principal.account_id,
                resource_id=revision.id,
                payload_json={"system_map_id": str(map_id), "revision_number": revision.revision_number},
            )
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_ARCHIVE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_ARCHIVE_SYSTEM_MAP_REVISION)
        _log.exception(_EP_ARCHIVE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.archive_revision")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SystemMapRevisionResponse.model_validate(revision)


@router.post(
    "/{map_id}/revisions/{rev_id}/diff",
    response_model=SystemMapRevisionDiffResponse,
)
@handle_db_errors(_EP_DIFF_SYSTEM_MAP_REVISION)
async def diff_system_map_revision_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    rev_id: uuid.UUID = Path(..., description=_DESC_REVISION_ID),
    req: SystemMapRevisionDiffRequest = Body(default=SystemMapRevisionDiffRequest()),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_REVISION_DIFF),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapRevisionDiffResponse:
    """Diff a revision against another revision or the current draft."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            # Verify base revision exists and belongs to map
            base_revision = await repository.get_revision_by_id(
                session, organisation_id=principal.organisation_id, revision_id=rev_id
            )
            if not base_revision or base_revision.system_map_id != map_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            # Determine target revision
            target_revision_id = req.against_revision_id
            if target_revision_id is None:
                # Try to find the current draft revision
                draft_revisions = await repository.list_revisions(
                    session,
                    organisation_id=principal.organisation_id,
                    system_map_id=map_id,
                    status="draft",
                    limit=1,
                )
                if not draft_revisions:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="No draft revision found to diff against; specify against_revision_id",
                    )
                target_revision_id = draft_revisions[0].id
            else:
                # Verify target revision exists and belongs to map
                target_revision = await repository.get_revision_by_id(
                    session, organisation_id=principal.organisation_id, revision_id=target_revision_id
                )
                if not target_revision or target_revision.system_map_id != map_id:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            diff_result = await repository.diff_revisions(
                session,
                organisation_id=principal.organisation_id,
                base_revision_id=rev_id,
                target_revision_id=target_revision_id,
            )
            if not diff_result:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not compute diff")
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_DIFF_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_DIFF_SYSTEM_MAP_REVISION)
        _log.exception(_EP_DIFF_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.diff_revision")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SystemMapRevisionDiffResponse(**diff_result)


@router.post(
    "/{map_id}/revisions/{rev_id}/restore-as-new",
    response_model=SystemMapRevisionRestoreAsNewResponse,
    dependencies=[
        Depends(
            audited(
                "system_map_revision_restored_as_new",
                "system_map_revision",
                principal_dep=get_current_tenant_user_or_api_key,
            )
        )
    ],
)
@handle_db_errors(_EP_RESTORE_SYSTEM_MAP_REVISION)
async def restore_system_map_revision_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    rev_id: uuid.UUID = Path(..., description=_DESC_REVISION_ID),
    req: SystemMapRevisionRestoreAsNewRequest = Body(default=SystemMapRevisionRestoreAsNewRequest()),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_REVISION_RESTORE),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapRevisionRestoreAsNewResponse:
    """Create a new draft revision from a published/archived revision."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            # Verify source revision exists and belongs to map
            source_revision = await repository.get_revision_by_id(
                session, organisation_id=principal.organisation_id, revision_id=rev_id, include_deleted=True
            )
            if not source_revision or source_revision.system_map_id != map_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            if source_revision.status == "draft":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot restore a draft revision as new; it is already editable",
                )

            # Create new draft from source
            new_revision = await repository.restore_revision_as_new_draft(
                session,
                organisation_id=principal.organisation_id,
                system_map_id=map_id,
                source_revision_id=rev_id,
                notes=req.notes.replace("{revision_number}", str(source_revision.revision_number)),
            )
            if not new_revision:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.revision.restored_as_new",
                account_id=principal.account_id,
                resource_id=new_revision.id,
                payload_json={
                    "system_map_id": str(map_id),
                    "source_revision_id": str(rev_id),
                    "source_revision_number": source_revision.revision_number,
                    "new_revision_number": new_revision.revision_number,
                },
            )
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_RESTORE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_RESTORE_SYSTEM_MAP_REVISION)
        _log.exception(_EP_RESTORE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.restore_revision")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return SystemMapRevisionRestoreAsNewResponse(
        new_revision=SystemMapRevisionResponse.model_validate(new_revision),
        source_revision_id=rev_id,
        source_revision_number=source_revision.revision_number,
    )


@router.delete(
    "/{map_id}/revisions/{rev_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            audited(
                "system_map_revision_deleted",
                "system_map_revision",
                principal_dep=get_current_tenant_user_or_api_key,
                fail_closed=True,
            )
        )
    ],
)
@handle_db_errors(_EP_DELETE_SYSTEM_MAP_REVISION)
async def delete_system_map_revision_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    rev_id: uuid.UUID = Path(..., description=_DESC_REVISION_ID),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_REVISION_DELETE),
    repository: SystemMapRepository = Depends(get_repo),
) -> None:
    """Soft delete revision."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            # Verify revision exists and belongs to map
            revision = await repository.get_revision_by_id(
                session, organisation_id=principal.organisation_id, revision_id=rev_id, include_deleted=True
            )
            if not revision or revision.system_map_id != map_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            # Cannot delete published revision directly - must archive first
            if revision.status == "published":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot delete published revision directly; archive it first",
                )

            revision.deleted_at = datetime.now(UTC)
            revision.deleted_by = principal.account_id
            await session.flush()

            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.revision.deleted",
                account_id=principal.account_id,
                resource_id=revision.id,
                payload_json={"system_map_id": str(map_id), "revision_number": revision.revision_number},
            )
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_DELETE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_DELETE_SYSTEM_MAP_REVISION)
        _log.exception(_EP_DELETE_SYSTEM_MAP_REVISION)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.delete_revision")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# ─── Overview (Aggregate Read Model) ───


@router.get("/{map_id}/overview")
@handle_db_errors(_EP_GET_SYSTEM_MAP_OVERVIEW)
async def get_system_map_overview_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_OVERVIEW),
    repository: SystemMapRepository = Depends(get_repo),
) -> SystemMapOverviewResponse:
    """Aggregate read model with node health, loop configs, and bottleneck detection."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            # Get published revision
            published_revision = await repository.get_published_revision(
                session, organisation_id=principal.organisation_id, system_map_id=map_id
            )
            if not published_revision:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No published revision found for this map",
                )

            # Get full graph
            graph = await repository.get_full_revision_graph(
                session, organisation_id=principal.organisation_id, revision_id=published_revision.id
            )
            if not graph:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_REVISION_NOT_FOUND)

            nodes = graph["nodes"]
            loop_configs = graph["loop_configs"]

            # Build node health from runs data (simplified - would query runs in production)
            node_health: list[SystemMapOverviewNodeHealth] = []
            for node in nodes:
                # In a real implementation, this would query run data for the node's pipeline
                health = SystemMapOverviewNodeHealth(
                    node_key=node.node_key,
                    node_type=node.node_type,
                    display_name=node.display_name,
                    health="unknown",
                    queue_depth=None,
                    throughput_per_min=None,
                    capacity_pressure=None,
                    failure_rate=None,
                    telemetry_coverage=None,
                    last_run_id=None,
                    last_run_status=None,
                    last_run_completed_at=None,
                )
                node_health.append(health)

            # Build loop config overview
            loop_config_overview: list[SystemMapOverviewLoopConfig] = []
            for lc in loop_configs:
                # In a real implementation, this would query admission attempts and reservations
                loop_overview = SystemMapOverviewLoopConfig(
                    node_key=lc.node_key,
                    target_signal=lc.target_signal,
                    observation_window_seconds=lc.observation_window_seconds,
                    action_pipeline_id=lc.action_pipeline_id,
                    budget_limit_usd=lc.budget_limit_usd,
                    cooldown_seconds=lc.cooldown_seconds,
                    outcome_metric=lc.outcome_metric,
                    last_admission_at=None,
                    last_admission_status=None,
                    cooldown_active=False,
                    cooldown_until=None,
                    budget_used_usd=Decimal(0),
                    budget_period_start=None,
                )
                loop_config_overview.append(loop_overview)

            # Detect bottlenecks (simplified heuristic)
            bottlenecks: list[SystemMapOverviewBottleneck] = []
            for node in nodes:
                if node.node_type == "improvement_loop":
                    # Check if loop config exists
                    matching_lc = next((lc for lc in loop_configs if lc.node_key == node.node_key), None)
                    if not matching_lc:
                        bottlenecks.append(
                            SystemMapOverviewBottleneck(
                                node_key=node.node_key,
                                node_type=node.node_type,
                                display_name=node.display_name,
                                kind="unobserved",
                                detail=(f"Improvement loop node '{node.node_key}' missing loop configuration"),
                                severity="high",
                            )
                        )

            return SystemMapOverviewResponse(
                system_map_id=map_id,
                revision_id=published_revision.id,
                revision_number=published_revision.revision_number,
                generated_at=datetime.now(UTC),
                nodes=node_health,
                loop_configs=loop_config_overview,
                bottlenecks=bottlenecks,
            )
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_GET_SYSTEM_MAP_OVERVIEW)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_GET_SYSTEM_MAP_OVERVIEW)
        _log.exception(_EP_GET_SYSTEM_MAP_OVERVIEW)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.overview")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


async def _validate_attribution_binding_node(
    session: AsyncSession,
    repository: SystemMapRepository,
    *,
    organisation_id: uuid.UUID,
    map_id: uuid.UUID,
    revision_id: uuid.UUID,
    req: AttributionBindingCreate,
) -> None:
    """Validate that the target node exists and matches the binding kind."""
    node = await repository.get_node_by_map_revision_key(
        session,
        organisation_id=organisation_id,
        system_map_id=map_id,
        revision_id=revision_id,
        node_key=req.node_key,
    )
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Node {req.node_key} does not exist in the published map revision",
        )

    expected_node_type = "pipeline" if req.binding_kind == "trigger" else "improvement_loop"
    if node.node_type != expected_node_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{req.binding_kind} bindings require a {expected_node_type} node; "
                f"node {req.node_key} is {node.node_type}"
            ),
        )
    if req.binding_kind == "trigger" and node.pipeline_id != req.pipeline_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Node {req.node_key} is bound to a different pipeline",
        )
    if req.binding_kind == "action_pipeline":
        await _validate_action_pipeline_binding(
            session,
            repository,
            organisation_id=organisation_id,
            revision_id=revision_id,
            req=req,
        )


async def _validate_action_pipeline_binding(
    session: AsyncSession,
    repository: SystemMapRepository,
    *,
    organisation_id: uuid.UUID,
    revision_id: uuid.UUID,
    req: AttributionBindingCreate,
) -> None:
    """Validate the loop config referenced by an action-pipeline binding."""
    loop_config = await repository.get_loop_config_by_node_key(
        session,
        organisation_id=organisation_id,
        revision_id=revision_id,
        node_key=req.node_key,
    )
    if loop_config is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Node {req.node_key} has no improvement-loop configuration",
        )
    if loop_config.action_pipeline_id != req.pipeline_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Node {req.node_key} is configured for a different action pipeline",
        )


async def _verify_attribution_binding_targets(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    trigger_id: uuid.UUID | None,
) -> None:
    """Verify the referenced pipeline (and optional trigger) exist in the org."""
    pipeline_stmt = select(Pipeline).where(
        Pipeline.id == pipeline_id,
        Pipeline.organisation_id == organisation_id,
        Pipeline.deleted_at.is_(None),
    )
    pipeline_result = await session.execute(pipeline_stmt)
    pipeline = pipeline_result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {pipeline_id} not found in this organisation",
        )

    if trigger_id:
        trigger_stmt = select(Trigger).where(
            Trigger.id == trigger_id,
            Trigger.pipeline_id == pipeline_id,
            Trigger.organisation_id == organisation_id,
            Trigger.deleted_at.is_(None),
        )
        trigger_result = await session.execute(trigger_stmt)
        trigger = trigger_result.scalar_one_or_none()
        if not trigger:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trigger {trigger_id} not found in this organisation",
            )


# ─── Attribution Bindings ───


@router.post(
    "/{map_id}/attribution-bindings",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(
            audited(
                "attribution_binding_created",
                "system_map_attribution_binding",
                principal_dep=get_current_tenant_user_or_api_key,
            )
        )
    ],
)
@handle_db_errors(_EP_CREATE_ATTRIBUTION_BINDING)
async def create_attribution_binding_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    req: AttributionBindingCreate = Body(...),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_BINDING_CREATE),
    repository: SystemMapRepository = Depends(get_repo),
) -> AttributionBindingResponse:
    """Create an attribution binding for a map node."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            published_revision = await repository.get_published_revision(
                session, organisation_id=principal.organisation_id, system_map_id=map_id
            )
            if published_revision is None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System map has no published revision")
            await _validate_attribution_binding_node(
                session,
                repository,
                organisation_id=principal.organisation_id,
                map_id=map_id,
                revision_id=published_revision.id,
                req=req,
            )
            await _verify_attribution_binding_targets(
                session,
                organisation_id=principal.organisation_id,
                pipeline_id=req.pipeline_id,
                trigger_id=req.trigger_id,
            )

            binding = await repository.create_attribution_binding(
                session,
                organisation_id=principal.organisation_id,
                pipeline_id=req.pipeline_id,
                system_map_id=map_id,
                node_key=req.node_key,
                binding_kind=req.binding_kind,
                trigger_id=req.trigger_id,
            )

            await _record_audit(
                session,
                org_id=principal.organisation_id,
                event_type="system_map.attribution_binding.created",
                account_id=principal.account_id,
                resource_id=binding.id,
                payload_json={
                    "system_map_id": str(map_id),
                    "pipeline_id": str(req.pipeline_id),
                    "node_key": req.node_key,
                },
            )
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_EP_CREATE_ATTRIBUTION_BINDING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except IntegrityError as exc:
        _log.exception(_EP_CREATE_ATTRIBUTION_BINDING)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attribution binding conflicts with existing binding",
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_CREATE_ATTRIBUTION_BINDING)
        _log.exception(_EP_CREATE_ATTRIBUTION_BINDING)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception:
        _log.exception("system_maps.create_binding")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return AttributionBindingResponse.model_validate(binding)


@router.get("/{map_id}/attribution-bindings")
@handle_db_errors(_EP_LIST_ATTRIBUTION_BINDINGS)
async def list_attribution_bindings_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    pipeline_id: uuid.UUID | None = Query(default=None),
    trigger_id: uuid.UUID | None = Query(default=None),
    binding_kind: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_LIST),
    repository: SystemMapRepository = Depends(get_repo),
) -> list[AttributionBindingResponse]:
    """List attribution bindings for a map."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            bindings = await repository.list_attribution_bindings(
                session,
                organisation_id=principal.organisation_id,
                pipeline_id=pipeline_id,
                trigger_id=trigger_id,
                system_map_id=map_id,
                binding_kind=binding_kind,
            )
    except ProgrammingError as exc:
        _log.exception(_EP_LIST_ATTRIBUTION_BINDINGS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_LIST_ATTRIBUTION_BINDINGS)
        _log.exception(_EP_LIST_ATTRIBUTION_BINDINGS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.list_bindings")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return [AttributionBindingResponse.model_validate(b) for b in bindings]


@router.delete(
    "/{map_id}/attribution-bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            audited(
                "attribution_binding_deleted",
                "system_map_attribution_binding",
                principal_dep=get_current_tenant_user_or_api_key,
                fail_closed=True,
            )
        )
    ],
)
@handle_db_errors(_EP_DELETE_ATTRIBUTION_BINDING)
async def delete_attribution_binding_endpoint(
    map_id: uuid.UUID = Path(..., description=_DESC_SYSTEM_MAP_ID),
    binding_id: uuid.UUID = Path(..., description="Attribution binding ID"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_SYSTEM_MAP_BINDING_DELETE),
    repository: SystemMapRepository = Depends(get_repo),
) -> None:
    """Soft delete an attribution binding."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Verify map exists
            system_map = await repository.get_map_by_id(
                session, organisation_id=principal.organisation_id, map_id=map_id
            )
            if not system_map:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SYSTEM_MAP_NOT_FOUND)

            deleted = await repository.delete_attribution_binding(
                session,
                organisation_id=principal.organisation_id,
                binding_id=binding_id,
                deleted_by=principal.account_id,
            )
            if deleted:
                await _record_audit(
                    session,
                    org_id=principal.organisation_id,
                    event_type="system_map.attribution_binding.deleted",
                    account_id=principal.account_id,
                    resource_id=binding_id,
                    payload_json={"system_map_id": str(map_id)},
                )
    except ProgrammingError as exc:
        _log.exception(_EP_DELETE_ATTRIBUTION_BINDING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _EP_DELETE_ATTRIBUTION_BINDING)
        _log.exception(_EP_DELETE_ATTRIBUTION_BINDING)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("system_maps.delete_binding")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_BINDING_NOT_FOUND)


# ─── Discovery endpoint ───

_CODE_SYSTEM_MAPS_DISCOVER = "system_maps.discover"


def _fingerprint(draft: Any) -> str:
    payload = {"nodes": list(draft.nodes), "relationships": list(draft.relationships)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stage_type(node: dict[str, Any]) -> str:
    """Map a discovery node to an honest lifecycle-map stage type.

    Trigger nodes are ``pipeline`` graph nodes but carry no Modulo pipeline of
    their own, so ``manual`` describes them accurately; connectors and folders
    are external systems and stay ``placeholder`` (never published as Modulo).
    """
    if node["node_type"] == "pipeline":
        return "manual" if node["config_json"].get("source_kind") == "trigger" else "modulo"
    return "placeholder"


async def _persist_discovery_draft(
    session: AsyncSession,
    *,
    principal: TenantPrincipal,
    name: str,
    description: str | None,
    raw_content: dict[str, Any],
) -> tuple[Any, uuid.UUID, int]:
    """Persist a discovery draft through the System Maps repository."""
    repository = SystemMapDiscoveryRepository()
    try:
        content = raw_content
        map_obj = await repository.get_or_create_draft(
            session,
            organisation_id=principal.organisation_id,
            account_id=principal.account_id,
            name=name,
            description=description,
        )
        map_obj, revision_id, revision_number = await repository.save_discovery_revision(
            session,
            organisation_id=principal.organisation_id,
            map_id=map_obj.id,
            content=content,
            updated_by=principal.account_id,
        )
    except SystemMapNameConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return map_obj, revision_id, revision_number


@router.post(
    "/discover",
    response_model=SystemMapDiscoveryReport,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(
            audited(
                "system_map_discovery_drafted",
                "system_map",
                principal_dep=get_current_tenant_user,
            )
        )
    ],
)
@handle_db_errors(_CODE_SYSTEM_MAPS_DISCOVER)
async def discover_system_map(
    request: SystemMapDiscoveryRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("system_maps_access"),
) -> SystemMapDiscoveryReport:
    """Inspect the organisation and persist an unpublished deterministic draft."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        pipelines = list(
            (
                await session.execute(
                    select(Pipeline).where(
                        Pipeline.organisation_id == principal.organisation_id,
                        Pipeline.deleted_at.is_(None),
                        Pipeline.archived_at.is_(None),
                    )
                )
            ).scalars()
        )
        triggers = list(
            (
                await session.execute(
                    select(Trigger).where(
                        Trigger.organisation_id == principal.organisation_id,
                        Trigger.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        connectors = list(
            (
                await session.execute(
                    select(ConnectorInstance).where(
                        ConnectorInstance.organisation_id == principal.organisation_id,
                        ConnectorInstance.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        folders = list(
            (
                await session.execute(
                    select(PipelineFolder).where(PipelineFolder.organisation_id == principal.organisation_id)
                )
            ).scalars()
        )
        sources = [
            *[
                DiscoverySource(
                    kind="pipeline",
                    identifier=str(pipeline.id),
                    name=pipeline.name,
                    folder_id=str(pipeline.folder_id) if pipeline.folder_id else None,
                    explicit_system_map_tag=bool(pipeline.run_context_defaults.get("system_map_tag")),
                )
                for pipeline in pipelines
            ],
            *[
                DiscoverySource(
                    kind="trigger",
                    identifier=str(trigger.id),
                    name=trigger.name,
                    pipeline_id=str(trigger.pipeline_id),
                    trigger_type=trigger.trigger_type,
                )
                for trigger in triggers
            ],
            *[
                DiscoverySource(
                    kind="connector",
                    identifier=str(connector.id),
                    name=connector.name,
                    connector_type_id=connector.connector_type_id,
                )
                for connector in connectors
            ],
            *[DiscoverySource(kind="folder", identifier=str(folder.id), name=folder.name) for folder in folders],
        ]
        draft = discover_draft(sources)
        raw_content = {
            "nodes": list(draft.nodes),
            "relationships": list(draft.relationships),
            "stages": [
                {
                    "id": node["node_key"],
                    "name": node["display_name"],
                    "type": _stage_type(node),
                    "pipeline_id": node.get("pipeline_id"),
                    "config_json": node["config_json"],
                }
                for node in draft.nodes
            ],
            "edges": [
                {
                    "id": f"{edge['source_node_key']}->{edge['target_node_key']}:{edge['relationship_type']}",
                    "source": edge["source_node_key"],
                    "target": edge["target_node_key"],
                    "description": edge["relationship_type"],
                    "config_json": edge["config_json"],
                }
                for edge in draft.relationships
            ],
            "notes": "System Maps discovery draft; inferred edges are suggestions and are not published.",
            "discovery_fingerprint": _fingerprint(draft),
            "discovery": {"warnings": list(draft.warnings), "skipped": list(draft.skipped)},
        }
        map_obj, revision_id, revision_number = await _persist_discovery_draft(
            session,
            principal=principal,
            name=request.name,
            description=request.description,
            raw_content=raw_content,
        )

    return SystemMapDiscoveryReport(
        sources_inspected=[
            SystemMapDiscoverySource(kind=source.kind, identifier=source.identifier, name=source.name)
            for source in sources
        ],
        node_count=draft.node_count,
        edge_count=draft.edge_count,
        inferred_nodes=list(draft.nodes),
        inferred_edges=list(draft.relationships),
        skipped=[SystemMapDiscoverySkipped(**item) for item in draft.skipped],
        warnings=list(draft.warnings),
        draft_map_id=map_obj.id,
        draft_revision_id=revision_id,
        draft_revision_number=revision_number,
        published=False,
    )
