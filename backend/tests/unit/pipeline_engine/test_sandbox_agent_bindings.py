"""FAR-592 (D6): the E2B dispatch path sees per-agent runner bindings.

Fake-sandbox acceptance (the established ``AsyncSandbox.create`` mock style):
the resolved binding lands in the sandbox envs between the profile/host
credentials and the node ``env_vars_extra``, and a resolution failure or a
Local-tier refusal raises BEFORE ``AsyncSandbox.create`` — the sandbox is
NEVER created (pre-claim, re-dispatch safe).
"""

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.pipeline_engine.node_runner as node_runner
from modulo.core.pipeline_engine.node_runner import (
    SandboxBindingResolutionError,
    SandboxQueueTimeoutError,
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


async def test_provisioning_watchdog_fires_on_stuck_dispatching(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-766 prove-the-fix: a sandbox stuck in ``dispatching`` —
    ``AsyncSandbox.create`` never returning (E2B provider degradation) — fails the
    node RETRYABLY within the provisioning bound instead of riding to the slow
    35-min nodeless sweep.

    ``sink`` is a coroutine that only returns after the bound, so the old
    ``min(sandbox_timeout, 120)`` create await plus a node-level watchdog that
    stands down on the ``dispatching`` marker would let this ride (no
    checkpoint, claimed-but-never-dispatched). The fix wraps the create await in
    a dedicated provisioning watchdog.
    """
    fn = make_sandbox_agent_fn(_script_node_def())

    async def _hang_create(**kwargs: object) -> Any:
        await asyncio.sleep(30)
        raise AssertionError("create must have been cancelled by the provisioning watchdog")

    monkeypatch.setattr(node_runner, "_sandbox_provisioning_timeout", lambda: 0.2)
    with (
        patch("e2b.AsyncSandbox.create", new=_hang_create),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        pytest.raises(SandboxQueueTimeoutError),
    ):
        await fn(_run_state())


async def test_healthy_provisioning_is_not_falsely_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-766: a HEALTHY (fast) provisioning must NOT be failed by the new
    provisioning watchdog — the bound applies to the create await only, and a
    sandbox that returns promptly completes normally."""
    fn = make_sandbox_agent_fn(_script_node_def())
    sandbox = _script_sandbox_mock()

    monkeypatch.setattr(node_runner, "_sandbox_provisioning_timeout", lambda: 0.2)
    monkeypatch.setattr(node_runner, "_sandbox_binding_resolve_timeout", lambda: 0.2)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"


async def test_binding_resolution_timeout_classifies_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-766: ``resolve_agent_bindings`` is now bounded. A resolution that
    never returns must classify as the RETRYABLE ``SandboxBindingResolutionError``
    (never the terminal ``harness.unknown`` path) and the sandbox is NEVER
    created (pre-claim, re-dispatch safe)."""
    fn = make_sandbox_agent_fn(_script_node_def())

    async def _hang_resolve(**kwargs: object) -> Any:
        await asyncio.sleep(30)
        raise AssertionError("resolve_agent_bindings must have been cancelled")

    monkeypatch.setattr(node_runner, "_sandbox_binding_resolve_timeout", lambda: 0.2)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=_script_sandbox_mock())) as create_mock,
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=_hang_resolve),
        pytest.raises(SandboxBindingResolutionError),
    ):
        await fn(_run_state())

    assert create_mock.call_count == 0


async def test_real_e2b_provider_error_surfaced_in_failure_envelope() -> None:
    """FAR-766: a real provider error from ``AsyncSandbox.create`` (an e2b
    SandboxException, e.g. the Aug-30 ``400: Timeout cannot be greater than 1
    hours``) is surfaced in the failure envelope — error_type = the provider
    exception class and error_message carries the provider detail, not a generic
    ``Sandbox agent execution failed``."""
    from e2b.exceptions import SandboxException

    fn = make_sandbox_agent_fn(_script_node_def())

    async def _raise_provider(**kwargs: object) -> Any:
        raise SandboxException("400: Timeout cannot be greater than 1 hours")

    with (
        patch("e2b.AsyncSandbox.create", new=_raise_provider),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "failed"
    assert result["output"]["error_type"] == "SandboxException"
    assert "400: Timeout cannot be greater than 1 hours" in result["output"]["error_message"]
