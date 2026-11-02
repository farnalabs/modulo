"""System Maps entity resolution + execution for ``modulo apply`` (FAR-2288).

System Maps identity is by name (unique per org). The map's revision content
is managed declaratively when declared, but only on CREATE: the API list
endpoint exposes no revision content, so apply cannot reconcile a revision on
an existing map. Only "draft" status is managed by apply — publish is an
explicit action via the API, not an implicit state transition. The revision
number is always auto-assigned by the server.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from modulo.cli.apply.executor import ApplyHttpError, _failure_message, _find
from modulo.cli.apply.models import SystemMapEntity
from modulo.cli.apply.plan import KIND_SYSTEM_MAP

if TYPE_CHECKING:
    from modulo.cli.apply.executor import ApplyExecutor
    from modulo.cli.apply.models import EntitySet

_log = logging.getLogger(__name__)


def _entity_map(current_entities: dict[str, Any], key: str) -> dict[str, Any]:
    """The named entity map, defaulting to an empty one when absent."""
    return current_entities.get(key) or {}


def _system_map_name_conflict(entity: SystemMapEntity, ambiguous_maps: dict[str, Any]) -> str | None:
    """Block reason when the system map's name is ambiguous in the org list."""
    matches = ambiguous_maps.get(entity.name)
    if matches is None:
        return None
    return f"system map {entity.name!r}: ambiguous name, {matches} matches"


def _system_map_block_reason(
    entity: SystemMapEntity,
    ambiguous_maps: dict[str, Any],
) -> str | None:
    """Pre-resolution block reason: an ambiguous system map name."""
    return _system_map_name_conflict(entity, ambiguous_maps)


def _plan_system_map_view(
    entity: SystemMapEntity,
    current_entities: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None] | str:
    """Plan one system map's desired view; returns the block reason (str) on failure."""
    view = entity.managed_view()
    return view, view.get("revision")


def build_desired_views(
    entity_set: EntitySet,
    current_entities: dict[str, dict[str, dict[str, Any]]],
    desired: dict[str, list[tuple[str, dict[str, Any]]]],
    blocked: list[tuple[str, str, str]],
    blocked_keys: set[tuple[str, str]],
) -> tuple[dict[str, list[tuple[str, dict[str, Any]]]], list[tuple[str, str, str]]]:
    """Plan-phase resolution for system map entities.

    System maps are blocked when:
    - their name is AMBIGUOUS in the fetched org list
    - (no cross-entity refs to validate for system maps currently)

    The desired view is hashed on the declared revision content.
    """
    ambiguous_maps = _entity_map(current_entities, "ambiguous_system_map_names")
    for entity in entity_set.system_maps:
        if (KIND_SYSTEM_MAP, entity.name) in blocked_keys:
            continue
        block_reason = _system_map_block_reason(entity, ambiguous_maps)
        if block_reason is not None:
            blocked.append((KIND_SYSTEM_MAP, entity.name, block_reason))
            continue
        planned = _plan_system_map_view(entity, current_entities)
        if isinstance(planned, str):
            blocked.append((KIND_SYSTEM_MAP, entity.name, planned))
            continue
        view, _revision = planned
        desired[KIND_SYSTEM_MAP].append((entity.name, view))
    return desired, blocked


def _create_payload(entity: SystemMapEntity) -> dict[str, Any]:
    """Build the POST /api/v1/system-maps body for a declared system map."""
    return entity.create_payload()


def _patch_payload(entity: SystemMapEntity) -> dict[str, Any]:
    """Build the PATCH /api/v1/system-maps/{id} body for a declared system map."""
    return entity.update_payload()


def _revision_payload(entity: SystemMapEntity) -> dict[str, Any] | None:
    """Build the POST /api/v1/system-maps/{map_id}/revisions body for a declared revision."""
    if entity.revision is None:
        return None
    return {
        "nodes": [n.model_dump(mode="json") for n in entity.revision.nodes],
        "relationships": [r.model_dump(mode="json") for r in entity.revision.relationships],
        "loop_configs": [lc.model_dump(mode="json") for lc in entity.revision.loop_configs],
        "notes": entity.revision.notes,
    }


def _apply_one_system_map(
    executor: ApplyExecutor,
    entity: SystemMapEntity,
    status: str,
    *,
    report: dict[str, list[dict[str, Any]]],
    current_system_maps: dict[str, dict[str, Any]],
    map_ids: dict[str, str],
) -> str:
    """Create or update ONE system map; returns its system map id.

    Raises ApplyHttpError / httpx.HTTPError / KeyError / ValueError on
    failure — the caller isolates each system map in its own try/except so one
    bad system map never aborts the run.

    Create: POST /api/v1/system-maps, then POST /api/v1/system-maps/{id}/revisions
    when the config declares a revision. Update: PATCH /api/v1/system-maps/{id}.

    For "created" status, adds the map id to map_ids immediately after creation
    so that a revision failure can clean it up.
    """
    if status == "created":
        response = executor._post("/api/v1/system-maps", _create_payload(entity))
        map_id = str(response["id"])
        # Add to map_ids immediately so a revision failure can clean it up
        map_ids[entity.name] = map_id
        # Create revision if declared
        if entity.revision is not None:
            rev_payload = _revision_payload(entity)
            if rev_payload is not None:
                executor._post(f"/api/v1/system-maps/{map_id}/revisions", rev_payload)
    else:
        current = current_system_maps[entity.name]
        map_id = str(current["id"])
        patch = _patch_payload(entity)
        if patch:
            executor._patch(f"/api/v1/system-maps/{map_id}", patch)
        # Revision content is create-only: the list endpoint exposes no revision
        # content to reconcile, so an update cannot decide whether a declared
        # revision differs. plan_entity emits an explicit warning when an
        # existing map declares a revision, so this is never a silent drop.
    return map_id


def _record_system_map_failure(
    report: dict[str, list[dict[str, Any]]],
    status: str,
    entry: dict[str, Any],
    entity: SystemMapEntity | None,
    exc: Exception,
) -> None:
    """Move a failed system map entry to ``report['failed']`` (isolated per map)."""
    message = _failure_message(exc)
    displayed_name = entity.name if entity else entry["name"]
    _log.exception("apply system map failed: %s: %s", displayed_name, message)
    report[status].remove(entry)
    report["failed"].append({"kind": KIND_SYSTEM_MAP, "name": entry["name"], "error": message})


def apply_system_maps(
    executor: ApplyExecutor,
    entities: dict[str, list[tuple[str, Any]]],
    current_entities: dict[str, dict[str, dict[str, Any]]],
    report: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    """Create/update system maps per the plan report, capturing failures.

    Per-map semantics (payload contents, revision handling) live in
    :func:`_apply_one_system_map`; this orchestrator owns phase order and
    failure isolation.

    Returns the system map name -> id map (current + created).
    """
    current_system_maps = current_entities.get(KIND_SYSTEM_MAP) or {}
    map_ids: dict[str, str] = {
        name: str(item["id"]) for name, item in current_system_maps.items() if item.get("id") is not None
    }
    for status in ("created", "updated"):
        for entry in tuple(report[status]):
            if entry["kind"] != KIND_SYSTEM_MAP:
                continue
            entity: SystemMapEntity | None = None
            try:
                entity = _find(entities[KIND_SYSTEM_MAP], entry["name"])
                assert isinstance(entity, SystemMapEntity)
                # For "created" status, add to map_ids immediately after map creation
                # (inside _apply_one_system_map). If revision fails, the map creation
                # succeeded so we keep the map id in map_ids.
                map_ids[entity.name] = _apply_one_system_map(
                    executor,
                    entity,
                    status,
                    report=report,
                    current_system_maps=current_system_maps,
                    map_ids=map_ids,
                )
            except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
                _record_system_map_failure(report, status, entry, entity, exc)
                # For a just-created map whose revision failed, we KEEP the map id in map_ids
                # because the map creation itself succeeded. The revision failure is
                # recorded in the report but doesn't invalidate the map.
    return map_ids


__all__ = [
    "apply_system_maps",
    "build_desired_views",
]
