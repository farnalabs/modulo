"""Cross-team resource binding enforcement (PRD §9.3).

A connector instance (or model backend) with ``visibility: team`` is only usable
within pipelines owned by the same team. Binding a team-private connector to a
pipeline owned by a different team is blocked at the pipeline-save command layer
with the named error ``connector_team_mismatch``.

Org-wide resources (``visibility: org``) are usable by any pipeline in the
organisation, so they never produce a mismatch.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator._types import try_parse_uuid, try_parse_uuids
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend

CONNECTOR_TEAM_MISMATCH = "connector_team_mismatch"
MODEL_BACKEND_TEAM_MISMATCH = "model_backend_team_mismatch"


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


def model_backend_team_mismatch(
    model_backend_visibility: str | None,
    model_backend_owner_team_id: uuid.UUID | None,
    pipeline_owner_team_id: uuid.UUID | None,
) -> bool:
    """Return True when a model-backend binding crosses team boundaries.

    PRD §9.3: a model backend with ``visibility: team`` is only usable by a
    pipeline owned by the *same* team, mirroring the connector rule. Org-wide
    model backends never mismatch.
    """
    if (model_backend_visibility or "org") != "team":
        return False
    if model_backend_owner_team_id is None:
        return True
    return model_backend_owner_team_id != pipeline_owner_team_id


@dataclass(frozen=True)
class ModelBackendTeamMismatch:
    """A team-private model backend pinned by a pipeline owned by a different team."""

    model_backend_id: uuid.UUID
    model_backend_name: str
    model_backend_owner_team_id: uuid.UUID | None
    pipeline_owner_team_id: uuid.UUID | None
    node_id: str | None = None


def model_backend_team_mismatch_detail(mismatches: list[ModelBackendTeamMismatch]) -> str:
    """Build the HTTP error detail for a set of model-backend mismatches.

    The message always starts with the machine-readable named error
    ``model_backend_team_mismatch`` so clients can branch on it.
    """
    parts = [
        (
            f"model backend '{m.model_backend_name}' (id={m.model_backend_id}) is team-private "
            f"(owner team {m.model_backend_owner_team_id}) but pipeline is owned by team "
            f"{m.pipeline_owner_team_id}"
        )
        for m in mismatches
    ]
    return f"{MODEL_BACKEND_TEAM_MISMATCH}: " + "; ".join(parts)


async def _find_team_scope_mismatches[MismatchT](
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    entries: list[dict[str, Any]],
    id_key: str,
    model: type[Any],
    check_mismatch: Callable[[str | None, uuid.UUID | None, uuid.UUID | None], bool],
    build_mismatch: Callable[[Any, uuid.UUID | None, str | None], MismatchT],
) -> list[MismatchT]:
    """Shared fetch-and-filter pattern for team-scoped binding enforcement.

    Looks up the org-scoped resource rows referenced by ``entries`` and keeps
    only those that the team-scope rule flags as cross-team. Resources that
    cannot be resolved (missing, or in another org) are ignored — the graph
    validator reports them separately.
    """
    if not entries:
        return []

    raw_ids = [entry.get(id_key) for entry in entries]
    parsed_ids, _ = try_parse_uuids(raw_ids)
    if not parsed_ids:
        return []

    rows = (
        (
            await session.execute(
                select(model).where(
                    model.organisation_id == org_id,
                    model.id.in_(parsed_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    found: dict[uuid.UUID, Any] = {r.id: r for r in rows}

    mismatches: list[MismatchT] = []
    for entry in entries:
        node_id = str(entry.get("node_id")) if entry.get("node_id") else None
        rid = try_parse_uuid(entry.get(id_key))
        if rid is None:
            continue
        resource = found.get(rid)
        if resource is None:
            continue
        if not check_mismatch(resource.visibility, resource.owner_team_id, pipeline_owner_team_id):
            continue
        mismatches.append(build_mismatch(resource, pipeline_owner_team_id, node_id))
    return mismatches


async def find_model_backend_team_mismatches(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    model_backend_pins: list[dict[str, Any]],
) -> list[ModelBackendTeamMismatch]:
    """Return cross-team model-backend pin violations for a graph save.

    ``model_backend_pins`` uses the snapshot pin shape:
    ``[{"node_id": ..., "model_backend_id": ...}, ...]``. Model backends that
    cannot be resolved (missing, or in another org) are ignored — the graph
    validator reports them separately.
    """
    return await _find_team_scope_mismatches(
        session,
        org_id=org_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        entries=model_backend_pins,
        id_key="model_backend_id",
        model=ModelBackend,
        check_mismatch=model_backend_team_mismatch,
        build_mismatch=_build_model_backend_mismatch,
    )


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
    return await _find_team_scope_mismatches(
        session,
        org_id=org_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        entries=connector_bindings,
        id_key="connector_instance_id",
        model=ConnectorInstance,
        check_mismatch=connector_team_mismatch,
        build_mismatch=_build_connector_mismatch,
    )


def _build_model_backend_mismatch(
    backend: Any, pipeline_owner_team_id: uuid.UUID | None, node_id: str | None
) -> ModelBackendTeamMismatch:
    return ModelBackendTeamMismatch(
        model_backend_id=backend.id,
        model_backend_name=backend.name,
        model_backend_owner_team_id=backend.owner_team_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        node_id=node_id,
    )


def _build_connector_mismatch(
    instance: Any, pipeline_owner_team_id: uuid.UUID | None, node_id: str | None
) -> ConnectorTeamMismatch:
    return ConnectorTeamMismatch(
        connector_id=instance.id,
        connector_name=instance.name,
        connector_owner_team_id=instance.owner_team_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        node_id=node_id,
    )
