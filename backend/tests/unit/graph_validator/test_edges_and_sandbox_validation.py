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
        "agent_prompt": "Do the thing",
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


def test_sandbox_script_mode_valid_no_agent_prompt():
    """FAR-296: script mode requires script_command and does NOT require agent_prompt."""
    node = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "mode": "script",
        "script_command": "python3 main.py",
        "template_id": "opencode",
        "timeout_seconds": 600,
    }
    graph = {"nodes": [node], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_COMMAND" not in _codes(result)
    assert result.is_valid


def test_sandbox_script_mode_missing_command_errors():
    """FAR-296: script mode without script_command is a hard error."""
    node = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "mode": "script",
        "agent_prompt": "ignored",
        "template_id": "opencode",
    }
    graph = {"nodes": [node], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_COMMAND" in _codes(result)
    assert not result.is_valid


def test_sandbox_both_commands_error():
    """FAR-296: agent_command and script_command together is invalid."""
    node = _sandbox_node()
    node["script_command"] = "python3 main.py"
    graph = {"nodes": [node], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_COMMAND" in _codes(result)
    assert not result.is_valid


def test_sandbox_invalid_mode_error():
    """FAR-296: an unknown mode value is a hard error."""
    node = _sandbox_node(mode="docker")
    graph = {"nodes": [node], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_COMMAND" in _codes(result)
    assert not result.is_valid


def test_sandbox_missing_template_warns():
    graph = {"nodes": [_sandbox_node(template_id=None)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_MISSING_TEMPLATE" in _codes(result)
    assert "SANDBOX_UNKNOWN_TEMPLATE" not in _codes(result)


def test_sandbox_modulo_opencode_template_is_valid():
    """The managed cache-warmed 'modulo-opencode' template is a known-good value."""
    graph = {"nodes": [_sandbox_node(template_id="modulo-opencode")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert not result.issues


def test_sandbox_unknown_template_warns():
    """A template_id outside the known-good set emits SANDBOX_UNKNOWN_TEMPLATE."""
    graph = {"nodes": [_sandbox_node(template_id="bogus-template")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    codes = _codes(result)
    assert "SANDBOX_UNKNOWN_TEMPLATE" in codes
    assert "SANDBOX_MISSING_TEMPLATE" not in codes
    assert result.is_valid  # warnings only


def test_sandbox_default_opencode_template_is_valid():
    """The default 'opencode' template remains a known-good value."""
    graph = {"nodes": [_sandbox_node(template_id="opencode")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert not result.issues


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


def test_sandbox_no_timeout_is_valid():
    """A sandbox node without timeout_seconds uses the default (no crash, no warning)."""
    graph = {"nodes": [_sandbox_node(timeout_seconds=None)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_TIMEOUT_BOUNDS" not in _codes(result)
    assert "SANDBOX_TIMEOUT_INVALID" not in _codes(result)
    assert result.is_valid


def test_sandbox_string_timeout_in_range_is_valid():
    """A numeric string timeout within range parses and passes."""
    graph = {"nodes": [_sandbox_node(timeout_seconds="600")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_TIMEOUT_BOUNDS" not in _codes(result)
    assert "SANDBOX_TIMEOUT_INVALID" not in _codes(result)


def test_sandbox_float_stall_timeout_is_valid():
    """A fractional stall_timeout_seconds (e.g. 2.5) validates as a positive
    number — it must NOT be truncated by int() (FAR-98 type consistency)."""
    graph = {"nodes": [_sandbox_node(stall_timeout_seconds=2.5)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_STALL_TIMEOUT_INVALID" not in _codes(result)
    assert "SANDBOX_STALL_TIMEOUT_GT_TIMEOUT" not in _codes(result)
    assert result.is_valid


def test_sandbox_float_stall_timeout_exceeding_timeout_warns():
    """A float stall timeout larger than timeout_seconds still trips the bound check."""
    graph = {"nodes": [_sandbox_node(timeout_seconds=600, stall_timeout_seconds=600.5)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_STALL_TIMEOUT_GT_TIMEOUT" in _codes(result)


def test_sandbox_stall_timeout_with_non_numeric_timeout_is_safe():
    """A non-numeric timeout_seconds alongside a valid stall timeout must not
    crash the cross-check — the unparseable timeout degrades to None and the
    stall-vs-timeout comparison is skipped."""
    graph = {"nodes": [_sandbox_node(timeout_seconds="abc", stall_timeout_seconds=2.5)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    codes = _codes(result)
    assert "SANDBOX_TIMEOUT_INVALID" in codes
    assert "SANDBOX_STALL_TIMEOUT_GT_TIMEOUT" not in codes
    assert result.is_valid  # warnings only


def test_sandbox_stall_timeout_without_timeout_is_safe():
    """stall_timeout_seconds with no timeout_seconds skips the bound cross-check."""
    graph = {"nodes": [_sandbox_node(timeout_seconds=None, stall_timeout_seconds=3600.0)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    codes = _codes(result)
    assert "SANDBOX_STALL_TIMEOUT_GT_TIMEOUT" not in codes
    assert "SANDBOX_STALL_TIMEOUT_INVALID" not in codes
    assert result.is_valid


def test_sandbox_non_positive_stall_timeout_warns():
    """Zero / negative stall_timeout_seconds is not a positive number."""
    for bad in (0, -5):
        graph = {"nodes": [_sandbox_node(stall_timeout_seconds=bad)], "edges": []}
        result = ValidationResult()
        GraphValidator._check_sandbox_agent_config(graph, result)
        assert "SANDBOX_STALL_TIMEOUT_INVALID" in _codes(result)


def test_sandbox_non_numeric_stall_timeout_warns():
    """A non-numeric stall_timeout_seconds is flagged as invalid."""
    graph = {"nodes": [_sandbox_node(stall_timeout_seconds="abc")], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_STALL_TIMEOUT_INVALID" in _codes(result)


def test_sandbox_non_dict_context_files_is_valid():
    """context_files that is not a dict (e.g. a list) is skipped safely."""
    graph = {"nodes": [_sandbox_node(context_files=[])], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_CONTEXT_PATH_RELATIVE" not in _codes(result)
    assert result.is_valid


def test_sandbox_non_dict_env_vars_is_valid():
    """env_vars that is not a dict (e.g. None) is skipped safely."""
    graph = {"nodes": [_sandbox_node(env_vars=None)], "edges": []}
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config(graph, result)
    assert "SANDBOX_RESERVED_ENV_VAR" not in _codes(result)
    assert result.is_valid


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
