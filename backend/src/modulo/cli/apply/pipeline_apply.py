"""Pipeline entity resolution + execution for ``modulo apply`` (FAR-681 slice 2).

Agent name-refs: config graph nodes reference agents by NAME; the executor
resolves names to ids via the fetched /agents map and BLOCKS the entity when
a referenced agent is missing (unmanaged reference — apply never auto-creates
agents). The resolved graph payload is normalised through the REAL API node /
edge models (the same validators the server runs on save) so the desired
canonical view matches the server's read-back byte-for-byte and the plan hash
is stable across runs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from modulo.cli.apply.executor import ApplyHttpError, _failure_message, _find
from modulo.cli.apply.models import ApplyEntityResolutionError, PipelineEntity
from modulo.cli.apply.plan import KIND_PIPELINE, canonical_hash

if TYPE_CHECKING:
    from modulo.cli.apply.executor import ApplyExecutor
    from modulo.cli.apply.models import EntitySet

_log = logging.getLogger(__name__)

_EMPTY_GRAPH: dict[str, list[dict[str, Any]]] = {"nodes": [], "edges": []}


def _normalized_node(node_data: dict[str, Any]) -> dict[str, Any]:
    """Config node dict -> server-shape dump (real API node model validators)."""
    from modulo.api.routes.pipelines import PipelineGraphNode

    return PipelineGraphNode.model_validate(node_data).model_dump(mode="json")


def _normalized_edge(edge_data: dict[str, Any]) -> dict[str, Any]:
    """Config edge dict -> server-shape dump (real API edge model validators)."""
    from modulo.api.routes.pipelines import PipelineGraphEdge

    return PipelineGraphEdge.model_validate(edge_data).model_dump(mode="json")


def normalize_current_graph(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a fetched /pipelines/{id}/graph payload to the hash shape.

    Validated with the API models using the READ context (legacy_read) so a
    legacy stored gate description does not fail the fetch — the read path on
    the server uses the same context. The hash-parity guarantee holds because
    the desired side is normalised through the same models (write context).
    """
    from modulo.api.routes.pipelines import PipelineGraphEdge, PipelineGraphNode

    return {
        "nodes": [
            PipelineGraphNode.model_validate(node).model_dump(mode="json") for node in payload.get("nodes") or []
        ],
        "edges": [
            PipelineGraphEdge.model_validate(edge, context={"legacy_read": True}).model_dump(mode="json")
            for edge in payload.get("edges") or []
        ],
    }


def resolve_graph(entity: PipelineEntity, agent_ids: dict[str, Any]) -> dict[str, Any]:
    """Resolve agent name-refs + normalise the declared graph.

    Raises ApplyEntityResolutionError when a referenced agent is missing, or
    ValueError (pydantic ValidationError included) when the resolved payload
    fails the API node/edge shape rules.
    """
    declared = entity.graph
    nodes_payload: list[dict[str, Any]] = []
    for node in declared.nodes if declared is not None else []:
        node_data = node.api_node_payload()
        if node.agent is not None:
            agent_id = agent_ids.get(node.agent)
            if agent_id is None:
                msg = (
                    f"agent {node.agent!r} not found in target org - declare the agent in this config "
                    "or create it first; apply never auto-creates agents"
                )
                raise ApplyEntityResolutionError(msg)
            node_data["agent_id"] = agent_id
        nodes_payload.append(_normalized_node(node_data))
    edges_payload = [
        _normalized_edge(edge.api_edge_payload()) for edge in (declared.edges if declared is not None else [])
    ]
    return {"nodes": nodes_payload, "edges": edges_payload}


def build_desired_views(
    entity_set: EntitySet,
    current_entities: dict[str, dict[str, dict[str, Any]]],
    desired: dict[str, list[tuple[str, dict[str, Any]]]],
    blocked: list[tuple[str, str, str]],
    blocked_keys: set[tuple[str, str]],
) -> tuple[dict[str, list[tuple[str, dict[str, Any]]]], list[tuple[str, str, str]]]:
    """Plan-phase resolution for pipeline entities (agent refs + shape).

    Failures are per-entity blocked entries; later entities still plan.
    """
    agents = current_entities.get("agents") or {}
    for entity in entity_set.pipelines:
        if (KIND_PIPELINE, entity.name) in blocked_keys:
            continue
        try:
            graph = resolve_graph(entity, agents) if entity.graph is not None else None
        except ApplyEntityResolutionError as exc:
            blocked.append((KIND_PIPELINE, entity.name, str(exc)))
            continue
        except ValueError as exc:
            blocked.append((KIND_PIPELINE, entity.name, f"invalid pipeline graph: {exc}"))
            continue
        desired[KIND_PIPELINE].append((entity.name, entity.managed_view(graph=graph)))
    return desired, blocked


def apply_pipelines(
    executor: ApplyExecutor,
    entities: dict[str, list[tuple[str, Any]]],
    current_entities: dict[str, dict[str, dict[str, Any]]],
    report: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    """Create/update pipelines per the plan report, capturing failures.

    Create: POST /pipelines, then a graph PATCH (PATCH /{id} graph_json)
    when the config declares a graph. Update: PATCH /{id} with the managed
    top-level fields; graph_json is included only when the graph hash
    differs from the fetched current graph (a graph write snapshots the
    pipeline, so an unchanged graph must not churn snapshots).

    Returns the pipeline name -> id map (current + created), consumed by the
    trigger phase to resolve (pipeline, name) identities.
    """
    agents = current_entities.get("agents") or {}
    current_pipelines = current_entities.get(KIND_PIPELINE) or {}
    pipeline_ids: dict[str, str] = {
        name: str(item["id"]) for name, item in current_pipelines.items() if item.get("id") is not None
    }
    for status in ("created", "updated"):
        for entry in list(report[status]):
            if entry["kind"] != KIND_PIPELINE:
                continue
            entity = None
            try:
                entity = _find(entities[KIND_PIPELINE], entry["name"])
                assert isinstance(entity, PipelineEntity)
                graph = resolve_graph(entity, agents) if entity.graph is not None else None
                if status == "created":
                    response = executor._post(
                        "/pipelines",
                        {
                            "name": entity.name,
                            "description": entity.description,
                            "max_concurrent_runs": entity.max_concurrent_runs,
                        },
                    )
                    pipeline_id = str(response["id"])
                    graph_differs = entity.graph is not None
                else:
                    current = current_pipelines[entity.name]
                    pipeline_id = str(current["id"])
                    current_graph = current.get("graph") or _EMPTY_GRAPH
                    graph_differs = (
                        entity.graph is not None
                        and graph is not None
                        and canonical_hash(graph) != canonical_hash(current_graph)
                    )
                pipeline_ids[entity.name] = pipeline_id
                patch_payload: dict[str, Any] = {
                    "description": entity.description,
                    "max_concurrent_runs": entity.max_concurrent_runs,
                }
                if entity.graph is not None and graph is not None and graph_differs:
                    patch_payload["graph_json"] = graph
                if status == "updated" or entity.graph is not None:
                    executor._patch(f"/pipelines/{pipeline_id}", patch_payload)
            except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
                message = _failure_message(exc)
                displayed_name = entity.name if entity else entry["name"]
                _log.warning("apply pipeline failed: %s: %s", displayed_name, message)
                report[status].remove(entry)
                report["failed"].append({"kind": KIND_PIPELINE, "name": entry["name"], "error": message})
    return pipeline_ids


__all__ = [
    "apply_pipelines",
    "build_desired_views",
    "normalize_current_graph",
    "resolve_graph",
]
