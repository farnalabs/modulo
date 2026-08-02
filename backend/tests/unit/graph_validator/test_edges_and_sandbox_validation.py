"""Unit tests for GraphValidator edge-level and sandbox agent config checks.

Covers ``_check_edges`` (GRAPH_* codes) and ``_check_sandbox_agent_config``
(SANDBOX_* codes), which run on every save-time validation.
"""

import uuid

from modulo.core.graph_validator import GraphValidator, ValidationResult


def _codes(result: ValidationResult) -> set[str]:
    return {i.code for i in result.issues}


# ---------------------------------------------------------------------------
# _check_edges — GRAPH_NO_EDGES
# ---------------------------------------------------------------------------


def test_edges_no_edges_warns():
    """A graph with nodes but no edges emits a GRAPH_NO_EDGES warning."""
    graph = {"nodes": [{"id": str(uuid.uuid4())}, {"id": str(uuid.uuid4())}], "edges": []}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    assert "GRAPH_NO_EDGES" in _codes(result)
    assert result.is_valid  # warnings only


def test_edges_single_node_warns_too():
    """A single-node graph with no edges also emits GRAPH_NO_EDGES."""
    graph = {"nodes": [{"id": str(uuid.uuid4())}], "edges": []}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    assert "GRAPH_NO_EDGES" in _codes(result)


def test_edges_no_warning_with_edges():
    """A graph with edges does not emit GRAPH_NO_EDGES."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    graph = {"nodes": [{"id": a}, {"id": b}], "edges": [{"source": a, "target": b, "type": "normal"}]}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    assert "GRAPH_NO_EDGES" not in _codes(result)


# ---------------------------------------------------------------------------
# _check_edges — GRAPH_DUPLICATE_NODE_ID
# ---------------------------------------------------------------------------


def test_duplicate_node_id_is_error():
    """Duplicate node IDs in the graph are a hard error."""
    nid = str(uuid.uuid4())
    graph = {"nodes": [{"id": nid}, {"id": nid}], "edges": []}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    assert "GRAPH_DUPLICATE_NODE_ID" in _codes(result)
    assert not result.is_valid


def test_duplicate_node_id_reports_duplicate_id():
    """The GRAPH_DUPLICATE_NODE_ID issue carries the duplicated node id."""
    nid = str(uuid.uuid4())
    graph = {"nodes": [{"id": nid}, {"id": nid}], "edges": []}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    dup = next(i for i in result.issues if i.code == "GRAPH_DUPLICATE_NODE_ID")
    assert dup.node_id == nid
    assert nid in dup.message


# ---------------------------------------------------------------------------
# _check_edges — GRAPH_NODE_ID_FORMAT
# ---------------------------------------------------------------------------


def test_non_uuid_node_id_warns():
    """Non-UUID-like node IDs produce a GRAPH_NODE_ID_FORMAT warning."""
    graph = {"nodes": [{"id": "node-1"}], "edges": []}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    assert "GRAPH_NODE_ID_FORMAT" in _codes(result)
    assert result.is_valid


def test_uuid_node_id_no_warning():
    """Proper UUID node IDs do not emit GRAPH_NODE_ID_FORMAT."""
    graph = {"nodes": [{"id": str(uuid.uuid4())}], "edges": []}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    assert "GRAPH_NODE_ID_FORMAT" not in _codes(result)


def test_node_without_id_skipped_in_format_check():
    """Nodes missing an id are skipped by the format check (no crash)."""
    graph = {"nodes": [{}], "edges": []}
    result = ValidationResult()
    GraphValidator._check_edges(graph, result)
    assert "GRAPH_NODE_ID_FORMAT" not in _codes(result)


# ---------------------------------------------------------------------------
# _check_sandbox_agent_config — errors and warnings
# ---------------------------------------------------------------------------


def _sandbox_node(**overrides) -> dict:
    node: dict = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "agent_command": "opencode run",
        "template_id": "opencode",
        "timeout_seconds": 600,
        "context_files": {"/workspace/input.txt": None},
        "env_vars": {"FOO": "bar"},
        "output_schema_json": {"type": "object"},
    }
    node.update(overrides)
    return node


def _non_sandbox_node() -> dict:
    return {"id": str(uuid.uuid4()), "node_type": "agent", "agent_command": ""}


def test_sandbox_non_sandbox_node_skipped():
    """Non-sandbox nodes are not validated by the sandbox check."""
    graph = {"nodes": [_non_sandbox_node()], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert not result.issues


def test_sandbox_valid_config_no_issues():
    """A fully populated sandbox agent node emits no issues."""
    graph = {"nodes": [_sandbox_node()], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert not result.issues


def test_sandbox_missing_command_errors():
    graph = {"nodes": [_sandbox_node(agent_command="")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_COMMAND" in _codes(result)
    assert not result.is_valid


def test_sandbox_whitespace_command_errors():
    graph = {"nodes": [_sandbox_node(agent_command="   ")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_COMMAND" in _codes(result)
    assert not result.is_valid


def test_sandbox_missing_template_warns():
    graph = {"nodes": [_sandbox_node(template_id=None)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_TEMPLATE" in _codes(result)


def test_sandbox_timeout_too_low_warns():
    graph = {"nodes": [_sandbox_node(timeout_seconds=10)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_TIMEOUT_BOUNDS" in _codes(result)


def test_sandbox_timeout_too_high_warns():
    graph = {"nodes": [_sandbox_node(timeout_seconds=7200)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_TIMEOUT_BOUNDS" in _codes(result)


def test_sandbox_timeout_invalid_warns():
    graph = {"nodes": [_sandbox_node(timeout_seconds="abc")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_TIMEOUT_INVALID" in _codes(result)


def test_sandbox_relative_context_file_warns():
    graph = {
        "nodes": [_sandbox_node(context_files={"relative/input.txt": None})],
        "edges": [],
    }
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_CONTEXT_PATH_RELATIVE" in _codes(result)


def test_sandbox_reserved_env_var_warns():
    graph = {"nodes": [_sandbox_node(env_vars={"MODULO_SECRET": "x"})], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_RESERVED_ENV_VAR" in _codes(result)


def test_sandbox_reserved_env_var_openai_api_key_warns():
    graph = {"nodes": [_sandbox_node(env_vars={"OPENCODE_API_KEY": "sk-x"})], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_RESERVED_ENV_VAR" in _codes(result)


def test_sandbox_output_schema_incomplete_warns():
    graph = {"nodes": [_sandbox_node(output_schema_json={"properties": {}})], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_SCHEMA_INCOMPLETE" in _codes(result)


def test_sandbox_multiple_issues_collected():
    """A badly configured sandbox node surfaces all issues at once."""
    graph = {
        "nodes": [
            _sandbox_node(
                agent_command="",
                template_id=None,
                timeout_seconds=5,
                context_files={"rel.txt": None},
                env_vars={"MODULO_X": "y"},
            )
        ],
        "edges": [],
    }
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    codes = _codes(result)
    assert {
        "SANDBOX_MISSING_COMMAND",
        "SANDBOX_MISSING_TEMPLATE",
        "SANDBOX_TIMEOUT_BOUNDS",
        "SANDBOX_CONTEXT_PATH_RELATIVE",
        "SANDBOX_RESERVED_ENV_VAR",
    }.issubset(codes)
    assert not result.is_valid  # missing agent_command is a hard error


# ---------------------------------------------------------------------------
# validate() end-to-end: sandbox errors block saves
# ---------------------------------------------------------------------------


async def test_validate_blocks_missing_sandbox_command():
    from unittest.mock import AsyncMock

    validator = GraphValidator()
    session = AsyncMock()
    snap = AsyncMock()
    snap.graph_json = {"nodes": [_sandbox_node(agent_command="")], "edges": []}
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = None

    result = await validator.validate(snap, session)
    assert "SANDBOX_MISSING_COMMAND" in _codes(result)
    assert not result.is_valid
