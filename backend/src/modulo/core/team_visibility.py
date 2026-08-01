"""Cross-team resource binding enforcement (PRD §9.3).

A connector instance (or model backend) with ``visibility: team`` is only usable
within pipelines owned by the same team. Binding a team-private connector to a
pipeline owned by a different team is blocked at the pipeline-save command layer
with the named error ``connector_team_mismatch``.

Org-wide resources (``visibility: org``) are usable by any pipeline in the
organisation, so they never produce a mismatch.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator._types import try_parse_uuid, try_parse_uuids
from modulo.db.models.connector_instance import ConnectorInstance

_log = logging.getLogger(__name__)

CONNECTOR_TEAM_MISMATCH = "connector_team_mismatch"


@dataclass(frozen=True)
class ConnectorTeamMismatch:
    """A team-private connector bound to a pipeline owned by a different team."""

    connector_id: uuid.UUID
    connector_name: str
    connector_owner_team_id: uuid.UUID | None
    pipeline_owner_team_id: uuid.UUID | None
    node_id: str | None = None


def connector_team_mismatch(
    connector_visibility: str | None,
    connector_owner_team_id: uuid.UUID | None,
    pipeline_owner_team_id: uuid.UUID | None,
) -> bool:
    """Return True when a connector binding crosses team boundaries.

    A team-private connector is only usable by a pipeline owned by the *same*
    team. A pipeline without an owning team (``owner_team_id=None``) or owned by
    a different team is a mismatch. Org-wide connectors never mismatch.
    """
    if (connector_visibility or "org") != "team":
        return False
    if connector_owner_team_id is None:
        return True
    return connector_owner_team_id != pipeline_owner_team_id


def connector_team_mismatch_detail(mismatches: list[ConnectorTeamMismatch]) -> str:
    """Build the HTTP error detail for a set of mismatches.

    The message always starts with the machine-readable named error
    ``connector_team_mismatch`` so clients can branch on it.
    """
    parts = [
        (
            f"connector '{m.connector_name}' (id={m.connector_id}) is team-private "
            f"(owner team {m.connector_owner_team_id}) but pipeline is owned by team "
            f"{m.pipeline_owner_team_id}"
        )
        for m in mismatches
    ]
    return f"{CONNECTOR_TEAM_MISMATCH}: " + "; ".join(parts)


def extract_connector_bindings(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract connector binding descriptors from graph node dicts.

    Returns the same shape used by snapshot ``connector_bindings_json``:
    ``[{"node_id": ..., "connector_instance_id": ...}, ...]``.
    """
    bindings: list[dict[str, Any]] = []
    for node in nodes:
        binding = node.get("connector_binding")
        if not isinstance(binding, dict):
            continue
        instance_id = binding.get("instance_id")
        if instance_id is None:
            continue
        bindings.append(
            {
                "node_id": str(node.get("id")),
                "connector_instance_id": str(instance_id),
            }
        )
    return bindings


async def find_connector_team_mismatches(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    connector_bindings: list[dict[str, Any]],
) -> list[ConnectorTeamMismatch]:
    """Return cross-team connector binding violations for a graph save.

    ``connector_bindings`` uses the same shape as snapshot
    ``connector_bindings_json`` entries: ``{"node_id": ..., "connector_instance_id": ...}``.
    Connectors that cannot be resolved (missing, or in another org) are ignored —
    the graph validator reports them separately as ``CONNECTOR_NOT_FOUND``.
    """
    if not connector_bindings:
        return []

    raw_ids = [b.get("connector_instance_id") for b in connector_bindings]
    instance_ids, _ = try_parse_uuids(raw_ids)
    if not instance_ids:
        return []

    rows = (
        (
            await session.execute(
                select(ConnectorInstance).where(
                    ConnectorInstance.organisation_id == org_id,
                    ConnectorInstance.id.in_(instance_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    found: dict[uuid.UUID, ConnectorInstance] = {r.id: r for r in rows}

    mismatches: list[ConnectorTeamMismatch] = []
    for binding in connector_bindings:
        node_id = str(binding.get("node_id")) if binding.get("node_id") else None
        cid = try_parse_uuid(binding.get("connector_instance_id"))
        if cid is None:
            continue
        instance = found.get(cid)
        if instance is None:
            continue
        if not connector_team_mismatch(instance.visibility, instance.owner_team_id, pipeline_owner_team_id):
            continue
        mismatches.append(
            ConnectorTeamMismatch(
                connector_id=instance.id,
                connector_name=instance.name,
                connector_owner_team_id=instance.owner_team_id,
                pipeline_owner_team_id=pipeline_owner_team_id,
                node_id=node_id,
            )
        )
    return mismatches
