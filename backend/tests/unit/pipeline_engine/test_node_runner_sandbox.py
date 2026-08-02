"""Unit tests for make_sandbox_agent_fn command resolution.

A sandbox_agent node MUST provide agent_command (or agent_commands);
there is no default command, and a missing command is a hard error.
"""

import pytest

from modulo.core.pipeline_engine.node_runner import make_sandbox_agent_fn


def test_missing_agent_command_raises_value_error():
    """A sandbox_agent node without agent_command/agent_commands is a hard error."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
    }
    with pytest.raises(ValueError, match="missing required 'agent_command'"):
        make_sandbox_agent_fn(node_def)


def test_missing_agent_commands_only_raises_value_error():
    """agent_commands with an empty list is the same as missing."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": [],
    }
    with pytest.raises(ValueError):
        make_sandbox_agent_fn(node_def)


async def test_with_agent_command_returns_callable():
    """A node_def with agent_command resolves without raising and returns a callable."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --auto --format json < /home/user/prompt.md",
    }
    fn = make_sandbox_agent_fn(node_def)
    assert callable(fn)


async def test_with_agent_commands_returns_callable():
    """agent_commands list is joined and resolved without raising."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": ["echo start", "opencode run"],
    }
    fn = make_sandbox_agent_fn(node_def)
    assert callable(fn)
