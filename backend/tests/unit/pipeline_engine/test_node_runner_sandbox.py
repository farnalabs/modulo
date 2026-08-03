"""Unit tests for make_sandbox_agent_fn command resolution.

A sandbox_agent node MUST provide agent_command (or agent_commands);
there is no default command, and a missing command is a hard error.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    _E2B_SANDBOX_USD_PER_HOUR,
    _compute_sandbox_cost,
    make_sandbox_agent_fn,
)


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


# ---------------------------------------------------------------------------
# Per-run agent runtime cost
# ---------------------------------------------------------------------------


def test_compute_sandbox_cost_hour_at_configured_rate():
    """3600s of sandbox uptime at the configured rate equals the rate itself.

    Default rate is 0.5 USD/hr, so one full hour of uptime estimates 0.5 USD.
    """
    expected = round(_E2B_SANDBOX_USD_PER_HOUR, 6)
    assert _compute_sandbox_cost(3600.0, None) == expected
    assert isinstance(_compute_sandbox_cost(3600.0, None), float)
    assert _compute_sandbox_cost(0.0, None) == 0.0


def test_compute_sandbox_cost_merges_agent_reported():
    """The agent's self-reported cost_estimate_usd is merged with the sandbox estimate."""
    # No sandbox uptime (elapsed 0) but agent reported 0.25 → total 0.25.
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": 0.25}) == 0.25
    # String numerics are accepted (JSON output can carry them).
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "0.25"}) == 0.25
    # Non-numeric / missing agent-reported values are ignored (contribute 0).
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "n/a"}) == 0.0
    assert _compute_sandbox_cost(0.0, {"summary": "no cost field"}) == 0.0
    assert _compute_sandbox_cost(0.0, None) == 0.0
    # Non-finite values (NaN/inf) must not corrupt the estimate.
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "nan"}) == 0.0
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "inf"}) == 0.0
    assert _compute_sandbox_cost(3600.0, {"cost_estimate_usd": float("inf")}) == round(_E2B_SANDBOX_USD_PER_HOUR, 6)


async def test_sandbox_agent_success_output_includes_cost_estimate_usd():
    """The success path attaches a numeric cost_estimate_usd to the node output.

    cost_estimate_usd = sandbox uptime x rate (tiny for a mocked instant run)
    + the agent's self-reported 0.001 from /home/user/output.json.
    """
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --auto --format json < /home/user/prompt.md",
    }
    fn = make_sandbox_agent_fn(node_def)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(return_value=cmd_result)
    sandbox.files.read = AsyncMock(return_value='{"summary": "done", "cost_estimate_usd": 0.001}')
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(
            {
                "run_context": {"input": {"task": "x"}},
                "_run_id": "run-1",
                "_pipeline_id": "pipe-1",
                "_org_id": "org-1",
            }
        )

    assert result["output"]["status"] == "completed"
    assert isinstance(result["output"]["cost_estimate_usd"], float)
    # sandbox uptime cost >= 0 plus the agent-reported 0.001.
    assert result["output"]["cost_estimate_usd"] >= 0.001
    # Artifact output mirrors the node output cost.
    assert result["artifacts"][0]["output"]["cost_estimate_usd"] == result["output"]["cost_estimate_usd"]
    assert isinstance(result["artifacts"][0]["output"]["cost_estimate_usd"], float)
