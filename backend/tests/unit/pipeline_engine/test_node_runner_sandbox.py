"""Unit tests for make_sandbox_agent_fn command resolution.

A sandbox_agent node MUST provide agent_command (or agent_commands);
there is no default command, and a missing command is a hard error.
"""

import logging
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    _E2B_SANDBOX_USD_PER_HOUR,
    _compute_sandbox_cost,
    make_sandbox_agent_fn,
    resolve_env_var_refs,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


def _make_sandbox_mock():
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(return_value=cmd_result)
    sandbox.files.read = AsyncMock(return_value='{"summary": "done"}')
    sandbox.kill = AsyncMock()
    return sandbox


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

    Default rate is 0.13 USD/hr, so one full hour of uptime estimates 0.13 USD.
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


# ---------------------------------------------------------------------------
# {{ secrets.KEY }} env var resolution (org vault -> host env fallback)
# ---------------------------------------------------------------------------


def _base_node_def(**overrides) -> dict:
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": _AGENT_COMMAND,
    }
    node_def.update(overrides)
    return node_def


def _run_state() -> dict:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


async def test_env_var_secret_ref_missing_resolves_to_empty_string(caplog):
    """No session_factory and no host env value -> '' plus a warning (legacy)."""
    node_def = _base_node_def(env_vars={"FOO": "{{ secrets.FOO }}"})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with (
        caplog.at_level(logging.WARNING),
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch.dict(os.environ, {}, clear=True),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == ""
    assert any("env_var.secret_ref_not_found" in m for m in caplog.messages)


async def test_env_var_secret_ref_resolves_from_vault():
    """With session_factory, {{ secrets.FOO }} resolves from the org vault."""
    node_def = _base_node_def(env_vars={"FOO": "{{ secrets.FOO }}"})
    session = MagicMock()
    session.__aenter__.return_value = session
    session.begin.return_value = AsyncMock()
    backend = MagicMock()
    backend.get_secret = AsyncMock(return_value="vault-secret")

    def _fake_session_factory():
        return session

    fn = make_sandbox_agent_fn(node_def, session_factory=_fake_session_factory)
    sandbox = _make_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="test-key")),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == "vault-secret"
    backend.get_secret.assert_awaited_once_with("FOO")


async def test_env_var_secret_ref_falls_back_to_host_env():
    """Vault raises KeyError but the host env has FOO -> host value wins."""
    node_def = _base_node_def(env_vars={"FOO": "{{ secrets.FOO }}"})
    session = MagicMock()
    session.__aenter__.return_value = session
    session.begin.return_value = AsyncMock()
    backend = MagicMock()
    backend.get_secret = AsyncMock(side_effect=KeyError("FOO"))

    def _fake_session_factory():
        return session

    fn = make_sandbox_agent_fn(node_def, session_factory=_fake_session_factory)
    sandbox = _make_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="test-key")),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch.dict(os.environ, {"FOO": "host-value"}),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == "host-value"


async def test_env_var_plain_value_passes_through():
    """Non-reference env var values are passed through unchanged."""
    node_def = _base_node_def(env_vars={"FOO": "plain-value"})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == "plain-value"


async def test_resolve_env_var_refs_calls_resolver_per_ref():
    """resolve_env_var_refs calls the async resolver only for secret refs."""
    calls: list[str] = []

    async def _resolver(secret_key: str) -> str | None:
        calls.append(secret_key)
        return {"A": "a-secret"}.get(secret_key)

    resolved = await resolve_env_var_refs({"A": "{{ secrets.A }}", "B": "plain", "C": "{{ secrets.C }}"}, _resolver)
    assert calls == ["A", "C"]
    assert resolved == {"A": "a-secret", "B": "plain", "C": ""}


async def test_sandbox_agent_command_timeout_surfaces_clear_summary():
    """A timed-out command (cmd_result None, exit_code -1, EMPTY stdout/stderr)
    must surface a clear explanation in the summary — not a silent empty-summary
    failure (the Branch Fixer empty-agent-output hang of 2026-08-05)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(side_effect=TimeoutError("no output.json"))
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    output = result["output"]
    artifact = result["artifacts"][0]["output"]
    assert output["status"] == "failed"
    assert artifact["exit_code"] == -1
    assert output["agent_stdout"] == ""
    assert output["agent_stderr"] == ""
    assert "no output" in output["summary"]
    assert "30s" in output["summary"]
    assert artifact["summary"] == output["summary"]
