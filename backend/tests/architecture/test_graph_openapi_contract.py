"""FAR-2475: Graph-specific OpenAPI contract assertions.

These tests lock the AUTHORITATIVE graph contract path:
Pydantic graph models -> FastAPI OpenAPI -> generated TypeScript (frontend).

Expected values are literals pinned here (not derived from the models), so a
drift in the model is a test failure rather than a self-fulfilling assertion.

No parallel JSON Schema or graph-schema workflow is introduced.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# The app import requires these env vars (mirrors tests/unit/api/conftest.py).
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    from modulo.api.main import app

    return app.openapi()


@pytest.fixture(scope="module")
def schemas(document: dict[str, Any]) -> dict[str, Any]:
    return document["components"]["schemas"]


# ---------------------------------------------------------------------------
# Expected literals pinned against the contract
# ---------------------------------------------------------------------------

_EXPECTED_NODE_REQUIRED = {"id", "position"}
_EXPECTED_NODE_TYPES = {
    "agent",
    "manual",
    "composite",
    "sandbox_agent",
    "router",
    "hitl",
    "join",
    "dispatch",
    "connector",
}
_EXPECTED_EDGE_TYPES = {"normal", "reject", "conditional", "loop"}
_EXPECTED_MODE_VALUES = {"llm", "script"}
_EXPECTED_EGRESS_POLICY = {"default", "deny_all", "selected"}
_EXPECTED_GIT_CREDENTIALS = {"scoped", "unscoped", "none"}
_EXPECTED_SCHEMA_PROFILE = {"verbatim", "provider-strict", "runtime-sdk"}
_EXPECTED_STDOUT_RETENTION = {"tail", "full"}
_EXPECTED_JOIN_PARTIAL = {"collect_and_proceed", "fail"}
_EXPECTED_OPERATION = {"query", "write", "dispatch"}
_EXPECTED_DISPATCH_ACTION = {"trigger_run", "get_run_status", "get_run_logs", "list_runs"}
_EXPECTED_POLICY_GATE_ACTION = {"warn", "block"}
_EXPECTED_FANOUT_SPLIT_REQUIRED = {"split"}
_EXPECTED_JOIN_COLLECT_REQUIRED = {"node"}
_EXPECTED_JOIN_AGGREGATE_REQUIRED = {"kind"}
_EXPECTED_JOIN_AGGREGATE_KIND = {"concat", "merge_by_key", "map", "custom_function"}

_EXPECTED_NODE_DEFAULT_DEFAULTS = {
    "node_type": "agent",
    "mode": "llm",
    "idempotent": True,
    "enable_heartbeat": True,
    "read_only": False,
    "single_pr_per_run": False,
    "await_completion": False,
    "join_partial_policy": "collect_and_proceed",
    "commands_concatenation_string": " && ",
}

_EXPECTED_EDGE_DEFAULT_DEFAULTS = {
    "source_port": "out",
    "target_port": "in",
    "edge_type": None,  # required, no default
}

# Fields that must stay nullable (anyOf string | null)
_EXPECTED_NODE_NULLABLE = {
    "agent_id",
    "agent_prompt",
    "agent_commands",
    "autonomy_recommendation",
    "capability_scope",
    "label",
    "role",
    "output_schema_id",
    "input_schema_pin",
    "output_schema_pin",
    "composite_ref",
    "composite_parameter_values",
    "composite_input_mapping",
    "composite_output_mapping",
    "parameter_set_id",
    "parameter_overrides",
    "template_id",
    "model_class",
    "egress_policy",
    "egress_allowlist",
    "resource_limits",
    "delivery_sentinel",
    "stall_timeout_seconds",
    "watch_log_path",
    "stdout_percentage_delta",
    "router_config",
    "hitl_config",
    "policy_gate",
    "fan_out",
    "collect",
    "aggregate",
    "timeout_seconds",
    "output_schema_json",
    "schema_profile",
    "description",
    "stdout_retention_mode",
    "stdout_max_bytes",
    "workspace_inputs",
    "wait_timeout",
    "git_credentials",
    "wallclock_budget_seconds",
    "context_files",
    "env_vars",
}

_EXPECTED_EDGE_NULLABLE = {
    "condition_expression",
    "hitl_review_config",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_graph_components_are_published(schemas: dict[str, Any]) -> None:
    """All four graph contract models exist in components."""
    for name in (
        "PipelineGraphNode",
        "PipelineGraphEdge",
        "PipelineGraphUpdate",
        "PipelineGraphResponse",
    ):
        assert name in schemas, f"missing component: {name}"


def test_node_required_fields(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    assert set(node["required"]) == _EXPECTED_NODE_REQUIRED


def test_edge_required_fields(schemas: dict[str, Any]) -> None:
    edge = schemas["PipelineGraphEdge"]
    assert set(edge["required"]) == {"source_node_id", "target_node_id", "edge_type"}


def test_update_required_fields(schemas: dict[str, Any]) -> None:
    update = schemas["PipelineGraphUpdate"]
    assert set(update["required"]) == {"nodes", "edges"}


def test_response_required_fields(schemas: dict[str, Any]) -> None:
    resp = schemas["PipelineGraphResponse"]
    assert set(resp["required"]) == {"nodes", "edges"}


def test_node_enum_node_type(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    enum = node["properties"]["node_type"]["enum"]
    assert set(enum) == _EXPECTED_NODE_TYPES


def test_node_enum_mode(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    enum = node["properties"]["mode"]["enum"]
    assert set(enum) == _EXPECTED_MODE_VALUES


def _extract_enum_from_nullable(prop: dict[str, Any]) -> list[str]:
    """Extract enum from a nullable field (anyOf with string enum + null)."""
    choices = prop.get("anyOf", [prop])
    obj = next((c for c in choices if c.get("type") == "string"), None)
    assert obj is not None, "expected string branch in anyOf"
    return obj.get("enum", [])


def test_node_enum_egress_policy(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    enum = _extract_enum_from_nullable(node["properties"]["egress_policy"])
    assert set(enum) == _EXPECTED_EGRESS_POLICY


def test_node_enum_git_credentials(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    enum = _extract_enum_from_nullable(node["properties"]["git_credentials"])
    assert set(enum) == _EXPECTED_GIT_CREDENTIALS


def test_node_enum_schema_profile(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    enum = _extract_enum_from_nullable(node["properties"]["schema_profile"])
    assert set(enum) == _EXPECTED_SCHEMA_PROFILE


def test_node_enum_stdout_retention(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    enum = _extract_enum_from_nullable(node["properties"]["stdout_retention_mode"])
    assert set(enum) == _EXPECTED_STDOUT_RETENTION


def test_node_enum_join_partial(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    enum = node["properties"]["join_partial_policy"]["enum"]
    assert set(enum) == _EXPECTED_JOIN_PARTIAL


def test_connector_binding_enums(schemas: dict[str, Any]) -> None:
    cb = schemas["ConnectorBinding"]
    assert set(cb["properties"]["operation"]["enum"]) == _EXPECTED_OPERATION
    assert set(cb["properties"]["dispatch_action"]["enum"]) == _EXPECTED_DISPATCH_ACTION


def test_policy_gate_enum(schemas: dict[str, Any]) -> None:
    pg = schemas["PipelineGraphPolicyGate"]
    assert set(pg["properties"]["action"]["enum"]) == _EXPECTED_POLICY_GATE_ACTION


def test_fanout_required(schemas: dict[str, Any]) -> None:
    fo = schemas["FanOutConfig"]
    assert set(fo["required"]) == _EXPECTED_FANOUT_SPLIT_REQUIRED


def test_join_collect_required(schemas: dict[str, Any]) -> None:
    jc = schemas["JoinCollectSpec"]
    assert set(jc["required"]) == _EXPECTED_JOIN_COLLECT_REQUIRED


def test_join_aggregate_required(schemas: dict[str, Any]) -> None:
    ja = schemas["JoinAggregateSpec"]
    assert set(ja["required"]) == _EXPECTED_JOIN_AGGREGATE_REQUIRED


def test_join_aggregate_kind_enum(schemas: dict[str, Any]) -> None:
    ja = schemas["JoinAggregateSpec"]
    enum = ja["properties"]["kind"]["enum"]
    assert set(enum) == _EXPECTED_JOIN_AGGREGATE_KIND


def test_edge_type_pattern(schemas: dict[str, Any]) -> None:
    edge = schemas["PipelineGraphEdge"]
    prop = edge["properties"]["edge_type"]
    assert "pattern" in prop
    # Pattern must match exactly the allowed types with anchors
    assert prop["pattern"] == "^(normal|reject|conditional|loop)$"


def test_edge_type_enum_values_not_used(schemas: dict[str, Any]) -> None:
    """edge_type uses a regex pattern, NOT an enum, so it cannot silently
    accept a value not in the set if the pattern changes."""
    edge = schemas["PipelineGraphEdge"]
    assert "enum" not in edge["properties"]["edge_type"]


def test_edge_defaults(schemas: dict[str, Any]) -> None:
    edge = schemas["PipelineGraphEdge"]
    # id is optional (default_factory)
    assert "id" not in edge["required"]
    assert edge["properties"]["source_port"]["default"] == "out"
    assert edge["properties"]["target_port"]["default"] == "in"


def test_node_defaults(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    props = node["properties"]
    for field, expected in _EXPECTED_NODE_DEFAULT_DEFAULTS.items():
        assert field in props, f"field {field} missing"
        default = props[field].get("default")
        assert default == expected, f"{field}: default {default!r} != {expected!r}"


def test_edge_defaults_explicit(schemas: dict[str, Any]) -> None:
    edge = schemas["PipelineGraphEdge"]
    props = edge["properties"]
    for field, expected in _EXPECTED_EDGE_DEFAULT_DEFAULTS.items():
        if expected is None:
            # required fields have no default
            assert field in edge.get("required", []), f"{field} should be required"
        else:
            assert props[field].get("default") == expected


def test_node_nullable_fields(schemas: dict[str, Any]) -> None:
    node = schemas["PipelineGraphNode"]
    for field in _EXPECTED_NODE_NULLABLE:
        prop = node["properties"].get(field)
        assert prop is not None, f"nullable field {field} missing"
        assert any(
            isinstance(choice, dict) and choice.get("type") == "null"
            for choice in prop.get("anyOf", [])
        ), f"field {field} is not nullable"


def test_edge_nullable_fields(schemas: dict[str, Any]) -> None:
    edge = schemas["PipelineGraphEdge"]
    for field in _EXPECTED_EDGE_NULLABLE:
        prop = edge["properties"].get(field)
        assert prop is not None, f"nullable field {field} missing"
        assert any(
            isinstance(choice, dict) and choice.get("type") == "null"
            for choice in prop.get("anyOf", [])
        ), f"field {field} is not nullable"


def test_write_response_models_differ_only_by_diagnostics(schemas: dict[str, Any]) -> None:
    update = schemas["PipelineGraphUpdate"]
    response = schemas["PipelineGraphResponse"]

    # Both require nodes + edges
    assert set(update["required"]) == {"nodes", "edges"}
    assert set(response["required"]) == {"nodes", "edges"}

    # Response adds diagnostics
    resp_props = response["properties"]
    assert "validation_issues" in resp_props
    assert "schema_translation_report" in resp_props

    # Nodes and edges refs are identical
    assert update["properties"]["nodes"] == response["properties"]["nodes"]
    assert update["properties"]["edges"] == response["properties"]["edges"]


def test_response_nodes_are_typed_not_untyped_dicts(schemas: dict[str, Any]) -> None:
    """The response's `nodes` array items must reference PipelineGraphNode,
    not degrade to an untyped dict. The field_serializer in
    PipelineGraphResponse uses a type COMMENT to preserve this."""
    resp = schemas["PipelineGraphResponse"]
    nodes_schema = resp["properties"]["nodes"]
    assert nodes_schema["items"]["$ref"] == "#/components/schemas/PipelineGraphNode"


def test_graph_routes_bind_the_contract(schemas: dict[str, Any], document: dict[str, Any]) -> None:
    """GET /{pipeline_id}/graph -> PipelineGraphResponse
    PUT /{pipeline_id}/graph body -> PipelineGraphUpdate
    PUT 200 -> PipelineGraphResponse"""
    paths = document.get("paths", {})
    path = paths.get("/api/v1/pipelines/{pipeline_id}/graph")
    assert path is not None, "graph route missing from OpenAPI"

    get_op = path.get("get")
    put_op = path.get("put")
    assert get_op is not None
    assert put_op is not None

    # GET 200 -> PipelineGraphResponse
    get_200 = get_op["responses"]["200"]
    assert get_200["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/PipelineGraphResponse"

    # PUT request body -> PipelineGraphUpdate
    put_body = put_op.get("requestBody")
    assert put_body is not None
    put_schema = put_body["content"]["application/json"]["schema"]
    assert put_schema["$ref"] == "#/components/schemas/PipelineGraphUpdate"

    # PUT 200 -> PipelineGraphResponse
    put_200 = put_op["responses"]["200"]
    assert put_200["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/PipelineGraphResponse"


def test_no_additional_properties_forbidden(schemas: dict[str, Any]) -> None:
    """Graph models use extra='ignore' so unknown keys are dropped, not rejected.
    This is the contract: additionalProperties must NOT be false."""
    graph_models = {
        "PipelineGraphNode",
        "PipelineGraphEdge",
        "PipelineGraphUpdate",
        "PipelineGraphResponse",
        "ConnectorBinding",
        "GraphPosition",
        "SchemaPin",
        "PipelineGraphPolicyGate",
        "FanOutConfig",
        "JoinCollectSpec",
        "JoinAggregateSpec",
        "CapabilityScope",
        "HitlReviewConfig",
        "HitlResponseContract",
        "HitlResponseOption",
    }
    for name in graph_models:
        comp = schemas.get(name)
        if comp is None:
            continue
        # The component's top-level additionalProperties must NOT be False
        addl = comp.get("additionalProperties")
        assert addl is not False, f"{name}: additionalProperties is False (extra='forbid' would be a breaking contract change)"


def test_node_object_properties_allow_extra(schemas: dict[str, Any]) -> None:
    """GraphPosition, ConnectorBinding, etc. use additionalProperties: true for dict fields."""
    # The dict-typed fields (filters, data, composite_parameter_values, etc.)
    # must carry additionalProperties that allows extra keys.
    node = schemas["PipelineGraphNode"]
    props = node["properties"]
    dict_fields = [
        "composite_parameter_values",
        "composite_input_mapping",
        "composite_output_mapping",
        "parameter_overrides",
        "egress_allowlist",
        "resource_limits",
        "output_schema_json",
        "context_files",
        "env_vars",
        "router_config",
        "hitl_config",
        "inputs",
        "outputs",
        "watch_globs",
    ]
    for field in dict_fields:
        prop = props.get(field)
        if prop is None:
            continue
        # nullable fields wrap in anyOf; find the object branch
        choices = prop.get("anyOf", [prop])
        obj = next((c for c in choices if c.get("type") == "object"), None)
        if obj is not None:
            addl = obj.get("additionalProperties")
            # Accept True, {} (empty object = true), or {"type": "string"} (string values allowed)
            assert addl is True or addl == {} or (isinstance(addl, dict) and addl.get("type") == "string"), (
                f"PipelineGraphNode.{field}: nested dict must allow extra keys, got {addl!r}"
            )


def test_edge_condition_expression_constraints(schemas: dict[str, Any]) -> None:
    edge = schemas["PipelineGraphEdge"]
    prop = edge["properties"]["condition_expression"]
    # maxLength 500, nullable
    choices = prop.get("anyOf", [prop])
    obj = next((c for c in choices if c.get("type") == "string"), None)
    assert obj is not None
    assert obj.get("maxLength") == 500


def test_connector_binding_required(schemas: dict[str, Any]) -> None:
    cb = schemas["ConnectorBinding"]
    assert set(cb["required"]) == {"type", "instance_id"}


def test_schema_pin_required(schemas: dict[str, Any]) -> None:
    sp = schemas["SchemaPin"]
    assert set(sp["required"]) == {"schema_id", "schema_version"}


def test_capability_scope_shape(schemas: dict[str, Any]) -> None:
    cs = schemas["CapabilityScope"]
    props = cs["properties"]
    assert "allowed_connectors" in props
    assert "allowed_tools" in props
    assert "context_scope" in props
    # All three are nullable arrays of strings
    for field in ("allowed_connectors", "allowed_tools", "context_scope"):
        prop = props[field]
        choices = prop.get("anyOf", [prop])
        obj = next((c for c in choices if c.get("type") == "array"), None)
        assert obj is not None
        assert obj["items"]["type"] == "string"


def test_fanout_shape(schemas: dict[str, Any]) -> None:
    fo = schemas["FanOutConfig"]
    props = fo["properties"]
    assert "split" in props
    assert "max_items" in props
    assert props["split"]["type"] == "string"


def test_join_collect_shape(schemas: dict[str, Any]) -> None:
    jc = schemas["JoinCollectSpec"]
    props = jc["properties"]
    assert "node" in props
    assert props["node"]["type"] == "string"


def test_join_aggregate_shape(schemas: dict[str, Any]) -> None:
    ja = schemas["JoinAggregateSpec"]
    props = ja["properties"]
    assert "kind" in props
    assert "key" in props
    assert "map_expression" in props


def test_graph_position_shape(schemas: dict[str, Any]) -> None:
    gp = schemas["GraphPosition"]
    assert set(gp["required"]) == {"x", "y"}
    assert gp["properties"]["x"]["type"] == "number"
    assert gp["properties"]["y"]["type"] == "number"


def test_validation_issue_shape(schemas: dict[str, Any]) -> None:
    vi = schemas["GraphValidationIssue"]
    assert set(vi["required"]) == {"severity", "code", "message"}
    assert vi["properties"]["severity"]["enum"] == ["error", "warning", "info"]
