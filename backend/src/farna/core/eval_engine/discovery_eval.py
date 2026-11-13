"""Deterministic System Maps discovery eval contract and adapter.

This adapter evaluates the existing pure ``discover_draft`` function. It owns
only case parsing, canonicalisation, exact comparison, and provenance; it does
not alter discovery rules or introduce model-mediated judgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

from farna.core.system_maps_discovery import DiscoverySource, discover_draft

DISCOVERY_EVAL_TYPE = "custom_function"
DISCOVERY_EVAL_FUNCTION = "system_map_discovery"
DISCOVERY_EVALUATOR_VERSION = "system-map-discovery-evaluator.v1"
DISCOVERY_CONTRACT_VERSION = "system-map-discovery-case.v1"
DISCOVERY_PROMPT_VERSION = "deterministic-no-prompt.v1"


class DiscoveryEvalError(ValueError):
    """A case cannot be evaluated under the versioned discovery contract."""

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or [{"path": [], "message": message}]


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: StrictStr
    identifier: StrictStr
    name: StrictStr
    folder_id: StrictStr | None = None
    pipeline_id: StrictStr | None = None
    trigger_type: StrictStr | None = None
    connector_type_id: StrictStr | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    explicit_system_map_tag: StrictBool = False

    def to_source(self) -> DiscoverySource:
        return DiscoverySource(**self.model_dump())


class _InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sources: list[_SourceModel]


_EXPECTED_KEYS = frozenset({"nodes", "relationships", "warnings", "skipped"})


@dataclass(frozen=True)
class DiscoveryEvaluation:
    passed: bool
    score: float
    detail: str
    actual: dict[str, Any] | None
    expected: dict[str, Any] | None
    diff: list[dict[str, Any]]
    provenance: dict[str, str]
    error: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "contract_version": DISCOVERY_CONTRACT_VERSION,
            "prompt_version": DISCOVERY_PROMPT_VERSION,
            "evaluator_version": DISCOVERY_EVALUATOR_VERSION,
            "passed": self.passed,
            "score": self.score,
            "detail": self.detail,
            "actual": self.actual,
            "expected": self.expected,
            "diff": self.diff,
            "provenance": self.provenance,
            "error": self.error,
        }


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"path": list(error["loc"]), "message": error["msg"]} for error in exc.errors()]


def parse_discovery_input(payload: Any) -> list[DiscoverySource]:
    """Parse the case input without coercing malformed values."""
    try:
        model = _InputModel.model_validate(payload)
    except ValidationError as exc:
        raise DiscoveryEvalError("invalid discovery input", errors=_validation_errors(exc)) from exc
    return [source.to_source() for source in model.sources]


def _canonical_graph(graph: Any) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise DiscoveryEvalError("expected_output must be an object")
    unknown = sorted(set(graph) - _EXPECTED_KEYS)
    missing = sorted(_EXPECTED_KEYS - set(graph))
    if unknown or missing:
        errors: list[dict[str, Any]] = []
        if unknown:
            errors.append({"path": [], "message": f"unknown fields: {unknown}"})
        if missing:
            errors.append({"path": [], "message": f"missing fields: {missing}"})
        raise DiscoveryEvalError(
            f"invalid discovery expected_output: {errors[0]['message']}",
            errors=errors,
        )
    nodes = graph["nodes"]
    relationships = graph["relationships"]
    warnings = graph["warnings"]
    skipped = graph["skipped"]
    if not all(isinstance(value, list) for value in (nodes, relationships, warnings, skipped)):
        raise DiscoveryEvalError("discovery graph collections must be lists")
    if any(not isinstance(item, dict) for item in nodes + relationships + skipped):
        raise DiscoveryEvalError("nodes, relationships and skipped entries must be objects")
    if any(not isinstance(item, str) for item in warnings):
        raise DiscoveryEvalError("warnings entries must be strings")
    canonical_nodes = sorted(nodes, key=lambda item: (str(item.get("node_key", "")), str(item)))
    edge_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in relationships:
        try:
            key = (edge["source_node_key"], edge["target_node_key"], edge["relationship_type"])
        except KeyError as exc:
            raise DiscoveryEvalError(f"relationship missing {exc.args[0]!r}") from exc
        edge_map[key] = edge
    canonical_edges = [edge_map[key] for key in sorted(edge_map)]
    canonical_skipped = sorted(skipped, key=lambda item: (str(item.get("source", "")), str(item.get("reason", ""))))
    return {
        "nodes": canonical_nodes,
        "relationships": canonical_edges,
        "warnings": sorted(set(warnings)),
        "skipped": canonical_skipped,
    }


def normalise_discovery_output(output: Any) -> dict[str, Any]:
    """Convert a DiscoveryDraft or JSON graph to the canonical case shape."""
    if hasattr(output, "nodes"):
        output = {
            "nodes": list(output.nodes),
            "relationships": list(output.relationships),
            "warnings": list(output.warnings),
            "skipped": list(output.skipped),
        }
    return _canonical_graph(output)


def _diff_mapping(expected: dict[str, Any], actual: dict[str, Any], path: tuple[str | int, ...]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for key in sorted(set(expected) | set(actual)):
        if key in expected and key in actual:
            differences.extend(_diff(expected[key], actual[key], (*path, key)))
        else:
            differences.append({"path": [*path, key], "expected": expected.get(key), "actual": actual.get(key)})
    return differences


def _diff_sequence(expected: list[Any], actual: list[Any], path: tuple[str | int, ...]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for index in range(max(len(expected), len(actual))):
        if index < len(expected) and index < len(actual):
            differences.extend(_diff(expected[index], actual[index], (*path, index)))
        else:
            differences.append(
                {
                    "path": [*path, index],
                    "expected": expected[index] if index < len(expected) else None,
                    "actual": actual[index] if index < len(actual) else None,
                }
            )
    return differences


def _diff(expected: Any, actual: Any, path: tuple[str | int, ...] = ()) -> list[dict[str, Any]]:
    if expected == actual:
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        return _diff_mapping(expected, actual, path)
    if isinstance(expected, list) and isinstance(actual, list):
        return _diff_sequence(expected, actual, path)
    return [{"path": list(path), "expected": expected, "actual": actual}]


def _provenance() -> dict[str, str]:
    return {
        "evaluator_version": DISCOVERY_EVALUATOR_VERSION,
        "discovery_algorithm_version": "system-maps-discovery.v1",
        "case_contract_version": DISCOVERY_CONTRACT_VERSION,
    }


def evaluate_discovery_case(input_payload: Any, expected_output: Any) -> DiscoveryEvaluation:
    """Run discovery and compare the exact canonical graph structure."""
    try:
        sources = parse_discovery_input(input_payload)
        expected = _canonical_graph(expected_output)
    except DiscoveryEvalError as exc:
        detail = f"discovery case error: {exc}"
        return DiscoveryEvaluation(
            passed=False,
            score=0.0,
            detail=detail,
            actual=None,
            expected=None,
            diff=exc.errors,
            provenance=_provenance(),
            error=True,
        )
    actual = normalise_discovery_output(discover_draft(sources))
    differences = _diff(expected, actual)
    detail = f"discovery graph mismatch ({len(differences)} difference(s)): {differences[0]['path']}" if differences else "discovery graph matched the expected output"
    return DiscoveryEvaluation(
        passed=not differences,
        score=1.0 if not differences else 0.0,
        detail=detail,
        actual=actual,
        expected=expected,
        diff=differences,
        provenance=_provenance(),
    )


__all__ = [
    "DISCOVERY_CONTRACT_VERSION",
    "DISCOVERY_EVALUATOR_VERSION",
    "DISCOVERY_EVAL_FUNCTION",
    "DISCOVERY_EVAL_TYPE",
    "DISCOVERY_PROMPT_VERSION",
    "DiscoveryEvalError",
    "DiscoveryEvaluation",
    "evaluate_discovery_case",
    "normalise_discovery_output",
    "parse_discovery_input",
]
