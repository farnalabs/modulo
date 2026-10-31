"""Deterministic System Maps discovery and draft generation.

This module deliberately contains no database or model-provider code. It turns
an organisation inventory into a stable, explainable draft graph. Persistence
and access control belong to the API/repository boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DiscoverySource:
    kind: str
    identifier: str
    name: str
    folder_id: str | None = None
    pipeline_id: str | None = None
    trigger_type: str | None = None
    connector_type_id: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    explicit_system_map_tag: bool = False


@dataclass(frozen=True)
class DiscoveryDraft:
    nodes: tuple[dict[str, Any], ...]
    relationships: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    skipped: tuple[dict[str, str], ...]

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.relationships)


def _node_key(source: DiscoverySource) -> str:
    return f"{source.kind}:{source.identifier}"


def _sorted_sources(sources: Iterable[DiscoverySource]) -> list[DiscoverySource]:
    return sorted(sources, key=lambda item: (item.kind, item.identifier, item.name))


def _relationship(
    source: str,
    target: str,
    relationship_type: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "source_node_key": source,
        "target_node_key": target,
        "relationship_type": relationship_type,
        "config_json": {"suggested": True, "reason": reason},
    }


_NODE_TYPES = {
    "pipeline": "pipeline",
    "trigger": "pipeline",
    "connector": "connector",
    "folder": "external_system",
}


def _build_node(source: DiscoverySource) -> dict[str, Any] | None:
    node_type = _NODE_TYPES.get(source.kind)
    if node_type is None:
        return None
    config: dict[str, Any] = {"source_kind": source.kind, "source_id": source.identifier}
    if source.kind == "folder":
        config["folder_id"] = source.identifier
    if source.kind == "trigger":
        config["trigger_type"] = source.trigger_type
    if source.kind == "connector":
        config["connector_type_id"] = source.connector_type_id
    return {
        "node_key": _node_key(source),
        "node_type": node_type,
        "display_name": source.name,
        "config_json": config,
        "pipeline_id": source.pipeline_id if source.kind == "pipeline" else None,
        "connector_instance_id": source.identifier if source.kind == "connector" else None,
        "owner_team_id": None,
        "position_x": 0,
        "position_y": 0,
    }


def _trigger_relationships(
    triggers: list[DiscoverySource],
    by_key: dict[str, DiscoverySource],
    skipped: list[dict[str, str]],
) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    for trigger in triggers:
        if not trigger.pipeline_id:
            skipped.append({"source": trigger.identifier, "reason": "trigger has no pipeline binding"})
            continue
        target = f"pipeline:{trigger.pipeline_id}"
        if target not in by_key:
            skipped.append({"source": trigger.identifier, "reason": "trigger pipeline is not in inventory"})
            continue
        relationships.append(
            _relationship(_node_key(trigger), target, "trigger", "trigger explicitly targets pipeline")
        )
    return relationships


def _folder_relationships(
    pipelines: list[DiscoverySource],
    folders: list[DiscoverySource],
    skipped: list[dict[str, str]],
) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    folder_ids = {folder.identifier for folder in folders}
    for pipeline in pipelines:
        if pipeline.folder_id is None:
            continue
        target = f"folder:{pipeline.folder_id}"
        if pipeline.folder_id not in folder_ids:
            skipped.append({"source": pipeline.identifier, "reason": "pipeline folder is not in inventory"})
            continue
        relationships.append(
            _relationship(target, _node_key(pipeline), "flow", "pipeline explicitly belongs to folder")
        )
    return relationships


def _improvement_loop_skips(pipelines: list[DiscoverySource]) -> list[dict[str, str]]:
    return [
        {
            "source": pipeline.identifier,
            "reason": "improvement loop has no supported non-self relationship",
        }
        for pipeline in pipelines
        if pipeline.name.startswith("improve-") or pipeline.explicit_system_map_tag
    ]


def _dedupe_relationships(relationships: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in relationships:
        key = (edge["source_node_key"], edge["target_node_key"], edge["relationship_type"])
        deduped[key] = edge
    return tuple(deduped[key] for key in sorted(deduped))


def discover_draft(sources: Iterable[DiscoverySource]) -> DiscoveryDraft:
    """Build a deterministic draft from explicitly supported relationships.

    The function never guesses from names or arbitrary connector configuration.
    A relationship is emitted only when both endpoints are present and its rule
    is explicit. Every emitted edge is marked ``suggested: true``.
    """
    ordered = _sorted_sources(sources)
    by_key = {_node_key(source): source for source in ordered}
    nodes: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped: list[dict[str, str]] = []

    for source in ordered:
        node = _build_node(source)
        if node is None:
            skipped.append({"source": source.identifier, "reason": "unsupported source kind"})
            continue
        nodes.append(node)

    pipelines = [source for source in ordered if source.kind == "pipeline"]
    triggers = [source for source in ordered if source.kind == "trigger"]
    folders = [source for source in ordered if source.kind == "folder"]

    relationships = [
        *_trigger_relationships(triggers, by_key, skipped),
        *_folder_relationships(pipelines, folders, skipped),
    ]
    skipped.extend(_improvement_loop_skips(pipelines))

    if not nodes:
        warnings.append("No supported organisation sources were available for discovery")
    if skipped:
        warnings.append(
            "Some organisation sources were skipped because their relationship was unsupported or ambiguous"
        )
    return DiscoveryDraft(
        nodes=tuple(nodes),
        relationships=_dedupe_relationships(relationships),
        warnings=tuple(sorted(set(warnings))),
        skipped=tuple(sorted(skipped, key=lambda item: (item["source"], item["reason"]))),
    )
