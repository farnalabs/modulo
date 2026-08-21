"""Unit tests for the MCP ``update_pipeline_graph`` sandbox gate (FAR-296/FAR-226).

``_validate_sandbox_nodes`` is the API-layer gate applied to the raw node
dicts of ``update_pipeline_graph`` before the graph is persisted. It routes
through the same shared helpers the Pydantic model, node runner, and
GraphValidator use, so save-time and run-time validation agree.

FAR-226: a sandbox_agent ``agent_command`` with broken Jinja syntax must be
rejected HERE (the MCP save path) as a clear config error — not surface later
as an opaque instant-fail for every run of the pipeline.
"""

import uuid

from modulo.api.mcp_server import _validate_sandbox_nodes


def _sandbox_node(**overrides: object) -> dict:
    node: dict = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run",
        "template_id": "opencode",
    }
    node.update(overrides)
    return node


def test_valid_llm_node_passes():
    """A well-formed llm-mode sandbox_agent node passes the gate."""
    assert _validate_sandbox_nodes([_sandbox_node()]) is None


def test_valid_jinja_template_passes():
    """A renderable agent_command with {{ }} references passes the gate."""
    assert (
        _validate_sandbox_nodes([_sandbox_node(agent_command="opencode run --model {{ input.model }} --auto")]) is None
    )


def test_broken_jinja_rejected_at_save_time():
    """FAR-226: an invalid backslash in agent_command is rejected by the MCP save path."""
    err = _validate_sandbox_nodes([_sandbox_node(agent_command="opencode --model {{ \\\\ }}")])
    assert err is not None
    assert err["error"] == "validation_failed"
    assert err["field"] == "nodes"
    assert "agent_command" in err["detail"]


def test_broken_jinja_rejected_in_agent_commands_list():
    """FAR-226: the joined agent_commands list form is validated too."""
    node = _sandbox_node(agent_command=None)
    node["agent_commands"] = ["opencode run", "--model {{ \\\\ }}"]
    err = _validate_sandbox_nodes([node])
    assert err is not None
    assert err["error"] == "validation_failed"
    assert "agent_command" in err["detail"]


def test_script_mode_not_jinja_checked():
    """FAR-226: script mode runs script_command verbatim — Jinja-ish content passes."""
    node = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "mode": "script",
        "script_command": "echo {{ not_a_template }}",
        "template_id": "opencode",
    }
    assert _validate_sandbox_nodes([node]) is None


def test_non_sandbox_nodes_skipped():
    """Only sandbox_agent nodes are validated by the gate."""
    agent = {"id": str(uuid.uuid4()), "node_type": "agent", "agent_command": "{{ \\\\ }}"}
    assert _validate_sandbox_nodes([agent]) is None


def test_mode_error_still_surfaced():
    """FAR-296 mode errors (both command kinds present) are surfaced by the gate."""
    node = _sandbox_node(script_command="python3 main.py")
    err = _validate_sandbox_nodes([node])
    assert err is not None
    assert err["error"] == "validation_failed"
    assert "mutually exclusive" in err["detail"]
