"""FAR-1743 / ADR 037 rev 2 — model_class registry read + mint resolution.

Covers the product-side resolution seam: the precedence chain (node
``model_class`` -> registry pipeline/node map -> registry pipeline default),
the ``_resolved_model`` / ``_model_origin`` bake, the fail-closed rejection
modes (unknown class / missing registry / unreadable or corrupt JSON), the
class-declared fallback as the ONLY soft path, and the render-side
``{{ model }}`` value helper (run override over baked value).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from modulo.core.model_class_registry import (
    MODEL_ORIGIN_NODE_MODEL_CLASS,
    MODEL_ORIGIN_REGISTRY_PIPELINE_DEFAULT,
    MODEL_ORIGIN_REGISTRY_PIPELINE_MAP,
    ModelClassResolutionError,
    effective_model_template_value,
    model_var_registry_coverage,
    node_references_model_var,
    resolve_graph_model_classes,
)


def _sandbox_node(node_id: str, **overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "node_type": "sandbox_agent",
        "template_id": "opencode",
        "agent_commands": ["opencode run --model {{ model }} --auto < /home/user/prompt.md"],
    }
    node.update(overrides)
    return node


def _write_registry(tmp_path: Path, payload: dict[str, Any]) -> str:
    path = tmp_path / "model-management.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


_DEFAULT_REGISTRY: dict[str, Any] = {
    "classes": {
        "builder": {"model": "opencode-go/mimo-v2.6-flash"},
        "planner": {"model": "opencode-go/deepseek-v4.1-flash"},
        "reviewer": {"model": "opencode-go/glm-5.3-flash"},
        "soft": {"fallback": "opencode-go/fallback-model"},
        "hollow": {"purpose": "declares neither model nor fallback"},
    },
    "pipelines_default": "builder",
}


# ---------------------------------------------------------------------------
# Precedence chain + bake
# ---------------------------------------------------------------------------


def test_node_model_class_wins_and_bakes_origin(tmp_path: Path) -> None:
    """A declared model_class resolves through the registry `classes` map and
    bakes `_resolved_model` + `_model_origin: node_model_class`."""
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    node = _sandbox_node("n1", model_class="planner")

    resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=registry_path)

    assert node["_resolved_model"] == "opencode-go/deepseek-v4.1-flash"
    assert node["_model_origin"] == MODEL_ORIGIN_NODE_MODEL_CLASS


def test_registry_pipeline_map_and_default_rungs_supply_class_for_unmapped_nodes(tmp_path: Path) -> None:
    """Nodes without model_class fall through to the registry `pipelines` map
    (per-node override, then pipeline entry) and finally `pipelines_default`."""
    registry_path = _write_registry(
        tmp_path,
        {
            **_DEFAULT_REGISTRY,
            "pipelines": {
                "Mapped Pipe": {"default": "planner", "nodes": {"n2": "reviewer"}},
                "String Pipe": "reviewer",
            },
        },
    )
    overridden = _sandbox_node("n2")
    sibling = _sandbox_node("n3")
    string_mapped = _sandbox_node("n4")
    unmapped = _sandbox_node("n5")

    resolve_graph_model_classes([overridden, sibling], pipeline_name="Mapped Pipe", registry_path=registry_path)
    resolve_graph_model_classes([string_mapped], pipeline_name="String Pipe", registry_path=registry_path)
    resolve_graph_model_classes([unmapped], pipeline_name="Unlisted Pipe", registry_path=registry_path)

    assert overridden["_resolved_model"] == "opencode-go/glm-5.3-flash"
    assert overridden["_model_origin"] == MODEL_ORIGIN_REGISTRY_PIPELINE_MAP
    assert sibling["_resolved_model"] == "opencode-go/deepseek-v4.1-flash"
    assert sibling["_model_origin"] == MODEL_ORIGIN_REGISTRY_PIPELINE_MAP
    # A bare string entry is the pipeline's class for every node in it.
    assert string_mapped["_resolved_model"] == "opencode-go/glm-5.3-flash"
    assert string_mapped["_model_origin"] == MODEL_ORIGIN_REGISTRY_PIPELINE_MAP
    assert unmapped["_resolved_model"] == "opencode-go/mimo-v2.6-flash"
    assert unmapped["_model_origin"] == MODEL_ORIGIN_REGISTRY_PIPELINE_DEFAULT


def test_no_registry_and_no_model_class_is_a_noop() -> None:
    """Back-compat: with no configured registry and no declared model_class,
    resolution engages nothing and changes nothing (existing pipelines run
    exactly as before)."""
    node = _sandbox_node("n1")

    resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=None)

    assert "_resolved_model" not in node
    assert "_model_origin" not in node


def test_excluded_pipelines_skip_registry_rungs(tmp_path: Path) -> None:
    """The registry's own excluded_pipelines list gates the auto-classification
    rungs — an unlisted node in an excluded pipeline stays unbaked."""
    registry_path = _write_registry(tmp_path, {**_DEFAULT_REGISTRY, "excluded_pipelines": ["Excluded Pipe"]})
    node = _sandbox_node("n1")

    resolve_graph_model_classes([node], pipeline_name="Excluded Pipe", registry_path=registry_path)

    assert "_resolved_model" not in node


def test_non_sandbox_nodes_are_never_touched(tmp_path: Path) -> None:
    """Only sandbox_agent nodes participate — agent nodes keep their own
    model_backend_id mechanism and other node types have no model at all."""
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    agent_node = {"id": "n1", "node_type": "agent", "model_class": "builder"}

    resolve_graph_model_classes([agent_node], pipeline_name="Any Pipeline", registry_path=registry_path)

    assert "_resolved_model" not in agent_node


# ---------------------------------------------------------------------------
# Bake gate: `_resolved_model` / `_model_origin` only for consumable nodes
# ---------------------------------------------------------------------------


def test_unreferenced_registry_rung_is_not_baked(tmp_path: Path) -> None:
    """No dead provenance (FAR-1743 fix): a registry-covered node whose
    commands never reference `{{ model }}` gets NO `_resolved_model` /
    `_model_origin` — the registry's intent must never be recorded as the
    model that ran. Before the gate this node WAS baked from
    ``pipelines_default`` even though nothing could ever consume the value."""
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    node = _sandbox_node("n1", agent_commands=["opencode run --model opencode-go/literal-model --auto"])

    resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=registry_path)

    assert "_resolved_model" not in node
    assert "_model_origin" not in node


def test_model_class_always_bakes_even_with_literal_commands(tmp_path: Path) -> None:
    """The gate's other arm: a declared ``model_class`` is the node's own
    opt-in to first-class resolution and always bakes, regardless of whether
    the command text references ``{{ model }}`` (the validator accepts the
    reference on such a node, and replay/audit read the baked value)."""
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    node = _sandbox_node("n1", model_class="planner", agent_commands=["opencode run --auto"])

    resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=registry_path)

    assert node["_resolved_model"] == "opencode-go/deepseek-v4.1-flash"
    assert node["_model_origin"] == MODEL_ORIGIN_NODE_MODEL_CLASS


# ---------------------------------------------------------------------------
# `{{ model }}` reference detection + validation-side registry coverage
# ---------------------------------------------------------------------------


def test_node_references_model_var_matches_only_the_top_level_variable() -> None:
    """The shared matcher: expressions and statement blocks that reference the
    standalone `model` identifier match; qualified references (input.model /
    run_context...model / parameter.model) and bound names (mymodel) do not."""
    assert node_references_model_var({"agent_commands": ["opencode run --model {{ model }} --auto"]})
    assert node_references_model_var({"agent_commands": ["opencode run"], "agent_prompt": "Use {{ model }}"})
    assert node_references_model_var({"agent_commands": ["{% if model %}opencode run{% endif %}"]})
    assert node_references_model_var({"agent_commands": "opencode run --model {{ model }}"})
    assert not node_references_model_var({"agent_commands": ["opencode run --model opencode-go/literal"]})
    assert not node_references_model_var({"agent_commands": ["{{ input.model }}", "{{ parameter.model }}"]})
    assert not node_references_model_var({"agent_commands": ["opencode run"], "agent_prompt": "Do the thing"})


def test_registry_coverage_map_default_and_exclusion(tmp_path: Path) -> None:
    """Coverage mirrors the mint rungs: a pipelines map entry (string /
    nodes override / in-entry default) or a valid pipelines_default covers;
    an unmapped pipeline with no default does not; excluded pipelines never
    are covered; no configured registry covers nothing."""
    registry_path = _write_registry(
        tmp_path,
        {
            **_DEFAULT_REGISTRY,
            "pipelines": {
                "Mapped Pipe": {"default": "planner", "nodes": {"n2": "reviewer"}},
                "String Pipe": "reviewer",
                "Nodes-Only Pipe": {"nodes": {"n9": "planner"}},
            },
            "excluded_pipelines": ["Excluded Pipe"],
        },
    )
    assert model_var_registry_coverage("Mapped Pipe", "n1", registry_path=registry_path) is True
    assert model_var_registry_coverage("String Pipe", "n1", registry_path=registry_path) is True
    # In a nodes-only entry (no default), an UNMAPPED node falls through to
    # pipelines_default — covered here; without a default it would not be.
    assert model_var_registry_coverage("Nodes-Only Pipe", "n9", registry_path=registry_path) is True
    assert model_var_registry_coverage("Nodes-Only Pipe", "other", registry_path=registry_path) is True
    assert model_var_registry_coverage("Unlisted Pipe", "n1", registry_path=registry_path) is True  # default rung
    assert model_var_registry_coverage("Excluded Pipe", "n1", registry_path=registry_path) is False
    assert model_var_registry_coverage("Any", "n1", registry_path=None) is False


def test_registry_coverage_without_pipelines_default(tmp_path: Path) -> None:
    """Map-only registry: an unmapped pipeline is NOT covered (False); a
    name-less query cannot consult the map, so it returns None (undeterminable)
    unless the name-independent pipelines_default covers."""
    registry_path = _write_registry(
        tmp_path,
        {
            "classes": {"builder": {"model": "opencode-go/mimo-v2.6-flash"}},
            "pipelines": {"Mapped Pipe": "builder"},
        },
    )
    assert model_var_registry_coverage("Mapped Pipe", "n1", registry_path=registry_path) is True
    assert model_var_registry_coverage("Unlisted Pipe", "n1", registry_path=registry_path) is False
    assert model_var_registry_coverage(None, "n1", registry_path=registry_path) is None


def test_registry_coverage_of_unreadable_registry_is_undeterminable(tmp_path: Path) -> None:
    """A configured-but-broken registry refuses every run closed at mint (D3),
    so coverage here is unknowable (None) — never a silent 'not covered'."""
    assert model_var_registry_coverage("Any Pipe", "n1", registry_path=str(tmp_path / "missing.json")) is None


# ---------------------------------------------------------------------------
# Fail closed (D3)
# ---------------------------------------------------------------------------


def test_unknown_class_fails_closed_naming_the_node(tmp_path: Path) -> None:
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    node = _sandbox_node("n1", model_class="does-not-exist")

    with pytest.raises(ModelClassResolutionError) as excinfo:
        resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=registry_path)

    detail = str(excinfo.value.detail)
    assert "does-not-exist" in detail
    assert "n1" in detail


def test_missing_registry_fails_closed_when_node_opts_in() -> None:
    node = _sandbox_node("n1", model_class="builder")

    with pytest.raises(ModelClassResolutionError) as excinfo:
        resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=None)

    assert "MODEL_CLASS_REGISTRY_PATH" in str(excinfo.value.detail)


def test_unreadable_registry_fails_closed(tmp_path: Path) -> None:
    node = _sandbox_node("n1", model_class="builder")

    with pytest.raises(ModelClassResolutionError) as excinfo:
        resolve_graph_model_classes(
            [node],
            pipeline_name="Any Pipeline",
            registry_path=str(tmp_path / "missing-model-management.json"),
        )

    assert "unreadable" in str(excinfo.value.detail)


def test_corrupt_registry_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "model-management.json"
    path.write_text("{not json", encoding="utf-8")
    node = _sandbox_node("n1", model_class="builder")

    with pytest.raises(ModelClassResolutionError) as excinfo:
        resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=str(path))

    assert "unreadable" in str(excinfo.value.detail)


def test_configured_but_corrupt_registry_fails_even_without_model_class(tmp_path: Path) -> None:
    """A CONFIGURED registry is part of the run's resolution chain (its
    pipeline map/default rungs apply to every sandbox node), so unreadable JSON
    rejects the run even when no node declared model_class (D3)."""
    path = tmp_path / "model-management.json"
    path.write_text("[]", encoding="utf-8")
    node = _sandbox_node("n1")

    with pytest.raises(ModelClassResolutionError) as excinfo:
        resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=str(path))

    assert "corrupt" in str(excinfo.value.detail)


def test_class_without_model_or_fallback_fails_closed(tmp_path: Path) -> None:
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    node = _sandbox_node("n1", model_class="hollow")

    with pytest.raises(ModelClassResolutionError) as excinfo:
        resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=registry_path)

    detail = str(excinfo.value.detail)
    assert "hollow" in detail
    assert "fallback" in detail


def test_malformed_model_class_value_fails_closed(tmp_path: Path) -> None:
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    node = _sandbox_node("n1", model_class="   ")

    with pytest.raises(ModelClassResolutionError):
        resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=registry_path)


# ---------------------------------------------------------------------------
# The ONLY soft path: a fallback the class itself declares
# ---------------------------------------------------------------------------


def test_class_declared_fallback_is_the_only_soft_path(tmp_path: Path) -> None:
    registry_path = _write_registry(tmp_path, _DEFAULT_REGISTRY)
    node = _sandbox_node("n1", model_class="soft")

    resolve_graph_model_classes([node], pipeline_name="Any Pipeline", registry_path=registry_path)

    assert node["_resolved_model"] == "opencode-go/fallback-model"
    assert node["_model_origin"] == MODEL_ORIGIN_NODE_MODEL_CLASS


# ---------------------------------------------------------------------------
# Render seam: {{ model }} = run override > baked value
# ---------------------------------------------------------------------------


def test_run_override_folds_in_over_the_baked_value() -> None:
    """Rung 1 (variant/run-time `_run_overrides.model`) beats the snapshot's
    baked `_resolved_model` — folded in pre-dispatch at render."""
    node_def = {"model_class": "builder", "_resolved_model": "opencode-go/baked-model"}
    run_context = {"_run_overrides": {"model": "opencode-go/variant-model"}}

    value = effective_model_template_value(node_def, run_context)

    assert value == "opencode-go/variant-model"


def test_baked_value_used_when_no_run_override() -> None:
    node_def = {"model_class": "builder", "_resolved_model": "opencode-go/baked-model"}

    value = effective_model_template_value(node_def, {"input": {"task": "x"}})

    assert value == "opencode-go/baked-model"


def test_no_value_neither_opted_in_nor_overridden() -> None:
    """A literal-only node with no run override has no `{{ model }}` value —
    the render site injects nothing (the validator already forbade any
    reference to it)."""
    value = effective_model_template_value({"id": "n1"}, {})

    assert value is None


def test_run_override_alone_supplies_the_variable_without_model_class() -> None:
    """The run override is a literal model id the variant system set — it
    resolves for the run regardless of the node's static binding."""
    value = effective_model_template_value({"id": "n1"}, {"_run_overrides": {"model": "opencode-go/variant-model"}})

    assert value == "opencode-go/variant-model"
