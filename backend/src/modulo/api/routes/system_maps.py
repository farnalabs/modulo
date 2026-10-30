"""Internal System Maps discovery endpoint."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from modulo.auth.jwt import TenantPrincipal
from modulo.core.system_maps_discovery import DiscoverySource, discover_draft
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_folder import PipelineFolder
from modulo.db.models.trigger import Trigger
from modulo.db.repositories.system_map import SystemMapRepository
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/v1/system-maps",
    tags=["system_maps"],
    dependencies=[Depends(_require_internal_system_maps_access), require_feature("system_maps")],
)


def _fingerprint(draft: Any) -> str:
    payload = {"nodes": list(draft.nodes), "relationships": list(draft.relationships)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@router.post("/discover", response_model=SystemMapDiscoveryReport, status_code=status.HTTP_201_CREATED)
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
        repository = SystemMapRepository()
        map_obj = await repository.get_or_create_draft(
            session,
            organisation_id=principal.organisation_id,
            account_id=principal.account_id,
            name=request.name,
            description=request.description,
        )
        content = {
            "stages": [
                {
                    "id": node["node_key"],
                    "name": node["display_name"],
                    "type": "modulo" if node["node_type"] == "pipeline" else "placeholder",
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
        map_obj, revision_id = await repository.save_discovery_revision(
            session,
            organisation_id=principal.organisation_id,
            map_id=map_obj.id,
            content=content,
            updated_by=principal.account_id,
        )
        revision_number = map_obj.version

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
