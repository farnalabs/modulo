"""FAR-592 (D6): the E2B dispatch path sees per-agent runner bindings.

Fake-sandbox acceptance (the established ``AsyncSandbox.create`` mock style):
the resolved binding lands in the sandbox envs between the profile/host
credentials and the node ``env_vars_extra``, and a resolution failure or a
Local-tier refusal raises BEFORE ``AsyncSandbox.create`` — the sandbox is
NEVER created (pre-claim, re-dispatch safe).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    SandboxBindingResolutionError,
    SandboxTierRefusedError,
    make_sandbox_agent_fn,
)
from modulo.core.runner_bindings import AgentBindingResolutionError, LocalProviderBindingsRefusedError

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _remote_e2b_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Script mode requires a remote E2B provider (same seam as the script tests)."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-e2b-key")


def _read_router(output_json: str):
    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return ""

    return _read


def _script_sandbox_mock(*, output_json: str = '{"result": "ok"}'):
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "script stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router(output_json))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    sandbox.get_metrics = AsyncMock(return_value=MagicMock(cpu_used_pct=1.0, mem_used=1, disk_used=1))
    return sandbox


def _run_state() -> dict:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": str(uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")),
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _script_node_def(**overrides) -> dict:
    node_def: dict = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "mode": "script",
        "script_command": "python3 /home/user/main.py",
        "agent_id": _AGENT_ID,
        "env_vars": {"GH_TOKEN": "node-token"},
    }
    node_def.update(overrides)
    return node_def


async def test_e2b_dispatch_sees_resolved_binding_and_node_wins() -> None:
    """The resolved binding lands in the envs; the node env_vars_extra WINS."""
    fn = make_sandbox_agent_fn(_script_node_def())
    sandbox = _script_sandbox_mock()

    async def _resolve(**kwargs):
        assert str(kwargs["agent_id"]) == _AGENT_ID
        assert str(kwargs["org_id"]) == _ORG_ID
        return {"OPENCODE_API_KEY": "stanza-secret", "GH_TOKEN": "binding-token"}

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=_resolve),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    create_mock.assert_called_once()
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["OPENCODE_API_KEY"] == "stanza-secret"
    # THE NODE WINS (reviewbot GITHUB_TOKEN override keeps working).
    assert envs["GH_TOKEN"] == "node-token"
    # The FAR-296 reserved path stays set after the merge.
    assert envs["MODULO_ORG_ID"] == _ORG_ID


async def test_local_tier_refusal_raises_before_sandbox_create() -> None:
    """A Local-tier refusal raises the typed error; the sandbox is NEVER created."""
    fn = make_sandbox_agent_fn(_script_node_def())
    sandbox = _script_sandbox_mock()

    async def _refuse(**kwargs):
        raise LocalProviderBindingsRefusedError("BindAgent")

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=_refuse),
        pytest.raises(SandboxTierRefusedError),
    ):
        await fn(_run_state())

    create_mock.assert_not_called()


async def test_resolution_failure_raises_before_sandbox_create() -> None:
    """A resolution failure raises the retryable error; the sandbox is NEVER created."""
    fn = make_sandbox_agent_fn(_script_node_def())
    sandbox = _script_sandbox_mock()

    async def _fail(**kwargs):
        raise AgentBindingResolutionError("source field 'api_key' unavailable for model backend 'X'")

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=_fail),
        pytest.raises(SandboxBindingResolutionError),
    ):
        await fn(_run_state())

    create_mock.assert_not_called()


async def test_generic_binding_resolution_error_classifies_retryable() -> None:
    """A bare Exception from resolve_agent_bindings (secrets-backend hard
    failure) hits the NEW generic branch and classifies as the RETRYABLE
    ``SandboxBindingResolutionError`` — never the terminal ``harness.unknown``
    path. The sandbox is NEVER created (pre-claim, re-dispatch safe)."""
    fn = make_sandbox_agent_fn(_script_node_def())
    sandbox = _script_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
        patch(
            "modulo.core.runner_bindings.resolve_agent_bindings",
            new=AsyncMock(side_effect=RuntimeError("secrets backend down")),
        ),
        pytest.raises(SandboxBindingResolutionError),
    ):
        await fn(_run_state())

    create_mock.assert_not_called()


async def test_agent_without_bindings_skips_resolution() -> None:
    """No agent_id on the node -> resolution is never invoked (zero overhead)."""
    node_def = _script_node_def()
    node_def.pop("agent_id")
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _script_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock()) as resolve_mock,
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    resolve_mock.assert_not_called()
