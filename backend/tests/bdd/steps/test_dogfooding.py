"""Step definitions for dogfooding pipeline feature."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.library.workflows.definitions import DOGFOODING_PIPELINE

scenarios("../../features/workflows/dogfooding.feature")

KNOWN_AGENTS: set[str] = {
    "changelog-aggregator",
    "changelog-writer",
    "code-reviewer",
    "compliance-checker",
    "correction-proposer",
    "dependency-analyzer",
    "doc-generator",
    "eval-proposal-writer",
    "feedback-analyzer",
    "migration-planner",
    "prd-summarizer",
    "prompt-improver",
    "release-note-generator",
    "rollback-planner",
    "schema-inferrer",
    "security-reviewer",
    "status-reporter",
    "test-generator",
    "ticket-estimator",
    "ticket-triager",
    "ticket-writer",
    "complexity-reviewer",
}

VALID_CONNECTOR_TYPES: set[str] = {
    "source_control",
    "ci_runner",
    "ci_cd",
    "incident_management",
    "issue_tracking",
    "filesystem",
    "monitoring",
    "messaging",
}


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {
        "step": None,
        "serialized": None,
        "deserialized": None,
    }


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("the dogfooding pipeline definition exists in the library")
def _dogfooding_pipeline_exists() -> None:
    assert DOGFOODING_PIPELINE is not None
    assert DOGFOODING_PIPELINE["name"] == "Dogfooding Pipeline"


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("the dogfooding pipeline definition is inspected")
def _inspect_pipeline(ctx: dict[str, Any]) -> None:
    ctx["pipeline"] = DOGFOODING_PIPELINE


@when("the dogfooding pipeline steps are inspected")
def _inspect_steps(ctx: dict[str, Any]) -> None:
    ctx["steps"] = DOGFOODING_PIPELINE["pipeline_steps"]


@when(parsers.parse('step "{step_id}" is inspected'))
def _inspect_step(ctx: dict[str, Any], step_id: str) -> None:
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        if step["id"] == step_id:
            ctx["step"] = step
            return
    pytest.fail(f"Step '{step_id}' not found")


@when("the dogfooding pipeline default config is inspected")
def _inspect_config(ctx: dict[str, Any]) -> None:
    ctx["config"] = DOGFOODING_PIPELINE["default_config"]


@when("all agent references in the dogfooding pipeline are checked")
def _check_agent_refs(ctx: dict[str, Any]) -> None:
    ctx["agent_refs_valid"] = True
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        agent = step.get("agent")
        if agent is not None and agent not in KNOWN_AGENTS:
            ctx["agent_refs_valid"] = False
            ctx["bad_agent_ref"] = (step["id"], agent)
            break


@when("all connector bindings in the dogfooding pipeline are checked")
def _check_connector_bindings(ctx: dict[str, Any]) -> None:
    ctx["connector_bindings_valid"] = True
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        binding = step.get("connector_binding")
        if binding is not None and binding["type"] not in VALID_CONNECTOR_TYPES:
            ctx["connector_bindings_valid"] = False
            ctx["bad_connector_binding"] = (step["id"], binding["type"])
            break


@when("the dependency chain of the dogfooding pipeline is validated")
def _validate_dependency_chain(ctx: dict[str, Any]) -> None:
    step_ids = {s["id"] for s in DOGFOODING_PIPELINE["pipeline_steps"]}
    ctx["deps_valid"] = True
    ctx["deps_orphans"] = []
    for step in DOGFOODING_PIPELINE["pipeline_steps"]:
        for dep in step.get("depends_on", []):
            if dep not in step_ids:
                ctx["deps_valid"] = False
                ctx["deps_orphans"].append((step["id"], dep))


@when("the dogfooding pipeline tags are inspected")
def _inspect_tags(ctx: dict[str, Any]) -> None:
    ctx["tags"] = DOGFOODING_PIPELINE["tags"]


@when("the dogfooding pipeline definition is serialised to JSON")
def _serialize_pipeline(ctx: dict[str, Any]) -> None:
    ctx["serialized"] = json.dumps(DOGFOODING_PIPELINE, default=str)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then('its name is "{name}"')
def _check_name(ctx: dict[str, Any], name: str) -> None:
    assert ctx["pipeline"]["name"] == name


@then('its version is "{version}"')
def _check_version(ctx: dict[str, Any], version: str) -> None:
    assert ctx["pipeline"]["version"] == version


@then('its author is "{author}"')
def _check_author(ctx: dict[str, Any], author: str) -> None:
    assert ctx["pipeline"]["author"] == author


@then("there are 5 steps")
def _check_step_count(ctx: dict[str, Any]) -> None:
    assert len(ctx["steps"]) == 5


@then(parsers.parse('the steps are in order: "{steps}"'))
def _check_step_order(ctx: dict[str, Any], steps: str) -> None:
    expected = [s.strip() for s in steps.split(",")]
    actual = [s["id"] for s in ctx["steps"]]
    assert actual == expected, f"Expected steps {expected}, got {actual}"


@then("it has no agent")
def _check_no_agent(ctx: dict[str, Any]) -> None:
    assert ctx["step"].get("agent") is None


@then(parsers.parse('it uses agent "{agent}"'))
def _check_agent(ctx: dict[str, Any], agent: str) -> None:
    assert ctx["step"]["agent"] == agent


@then(parsers.parse('it depends on "{dep}"'))
def _check_depends_on(ctx: dict[str, Any], dep: str) -> None:
    assert dep in ctx["step"].get("depends_on", [])


@then(parsers.parse('it has a connector binding of type "{ctype}" that is required'))
def _check_connector_required(ctx: dict[str, Any], ctype: str) -> None:
    assert ctx["step"]["connector_binding"] == {"type": ctype, "required": True}


@then(parsers.parse('it has a connector binding of type "{ctype}" that is optional'))
def _check_connector_optional(ctx: dict[str, Any], ctype: str) -> None:
    assert ctx["step"]["connector_binding"] == {"type": ctype, "required": False}


@then("it contains all expected default config keys")
def _check_config_keys(ctx: dict[str, Any]) -> None:
    expected = {
        "repo_owner",
        "repo_name",
        "base_branch",
        "branch_prefix",
        "require_human_approval",
        "auto_create_pr",
        "pr_template",
        "test_command",
        "notification_channels",
    }
    assert ctx["config"].keys() == expected, f"Expected keys {expected}, got {set(ctx['config'].keys())}"


@then("every referenced agent exists in the known agents set")
def _check_agent_refs_valid(ctx: dict[str, Any]) -> None:
    assert ctx.get("agent_refs_valid", False), f"Unknown agent '{ctx.get('bad_agent_ref')}' referenced in pipeline"


@then("every connector type is a valid known type")
def _check_connector_types_valid(ctx: dict[str, Any]) -> None:
    assert ctx.get("connector_bindings_valid", False), f"Invalid connector type '{ctx.get('bad_connector_binding')}'"


@then("every dependency reference points to an existing step")
def _check_deps_exist(ctx: dict[str, Any]) -> None:
    assert ctx.get("deps_valid", False), f"Orphaned dependencies: {ctx.get('deps_orphans')}"


@then("there are no circular dependencies")
def _check_no_circular_deps(ctx: dict[str, Any]) -> None:
    steps = DOGFOODING_PIPELINE["pipeline_steps"]
    step_ids = {s["id"] for s in steps}
    dep_graph = {s["id"]: set(s.get("depends_on", [])) for s in steps}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _has_cycle(node: str) -> bool:
        visited.add(node)
        in_stack.add(node)
        for dep in dep_graph.get(node, set()):
            if dep not in visited:
                if _has_cycle(dep):
                    return True
            elif dep in in_stack:
                return True
        in_stack.discard(node)
        return False

    for sid in step_ids:
        if sid not in visited:
            assert not _has_cycle(sid), f"Circular dependency detected involving step '{sid}'"


@then('the tags include "{tag}"')
def _check_tag(ctx: dict[str, Any], tag: str) -> None:
    assert tag in ctx["tags"], f"Expected tag '{tag}' not found in {ctx['tags']}"


@then("it can be deserialised without data loss")
def _check_deserialize(ctx: dict[str, Any]) -> None:
    ctx["deserialized"] = json.loads(ctx["serialized"])


@then('the deserialised name is "{name}"')
def _check_deserialized_name(ctx: dict[str, Any], name: str) -> None:
    assert ctx["deserialized"]["name"] == name
