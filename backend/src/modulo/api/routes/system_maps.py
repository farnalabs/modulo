"""Internal System Maps discovery endpoint."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    _require_internal_system_maps_access,
    get_db_session,
    require_feature,
    require_permission,
)
from modulo.api.schemas.system_maps import (
    SystemMapDiscoveryReport,
    SystemMapDiscoveryRequest,
    SystemMapDiscoverySkipped,
    SystemMapDiscoverySource,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.lifecycle_map.validation import (
    LifecycleMapContentError,
    LifecycleMapPipelineConflictError,
    normalize_content,
)
from modulo.core.system_map_persistence import SystemMapNameConflictError, SystemMapRepository
from modulo.core.system_maps_discovery import DiscoverySource, discover_draft
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_folder import PipelineFolder
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_SYSTEM_MAPS_DISCOVER = "system_maps.discover"

_log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/v1/system-maps",
    tags=["system_maps"],
    dependencies=[Depends(_require_internal_system_maps_access), require_feature("system_maps")],
)


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
    """Persist a draft, mapping domain refusals to their HTTP status.

    ``handle_db_errors`` on the route owns the SQLAlchemy/IntegrityError arms;
    this helper translates the domain errors that normalisation and the
    repository can raise. Normalisation MUST live inside this ``try``: a draft
    assembled from org data can still violate the lifecycle-map content bounds
    (e.g. an over-long source name), and that refusal is a 422, not a 500.
    """
    repository = SystemMapRepository()
    try:
        content = normalize_content(raw_content)
        map_obj = await repository.get_or_create_draft(
            session,
            organisation_id=principal.organisation_id,
            account_id=principal.account_id,
            name=name,
            description=description,
        )
        map_obj, revision_id = await repository.save_discovery_revision(
            session,
            organisation_id=principal.organisation_id,
            map_id=map_obj.id,
            content=content,
            updated_by=principal.account_id,
        )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except SystemMapNameConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    return map_obj, revision_id, map_obj.version


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
