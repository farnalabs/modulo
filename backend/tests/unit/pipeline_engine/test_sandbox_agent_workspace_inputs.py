"""FAR-800: managed workspace-input wiring inside _sandbox_agent_impl.

Covers the three integration blocks added to node_runner.py for managed
workspace inputs:

  - host-side ref resolution (before sandbox creation)
  - in-sandbox provisioning (after context files, before the agent command)
  - post-agent drift detection (attached to the result envelope)

plus their failure paths. Uses the same mocked ``AsyncSandbox.create`` style
as test_sandbox_agent_bindings.py.
"""

import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    make_sandbox_agent_fn,
)
from modulo.core.pipeline_engine.workspace_input_orchestration import (
    DriftResult,
    ProvisioningError,
    ResolvedInput,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _remote_e2b_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Script mode requires a remote E2B provider (same seam as the bindings tests)."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-e2b-key")
    # FAR-802: these tests exercise the managed-workspace-inputs path, which is
    # gated behind the MODULO_WORKSPACE_INPUTS_ENABLED kill-switch (OFF by
    # default). Enable it so the happy/failure paths actually run.
    monkeypatch.setenv("MODULO_WORKSPACE_INPUTS_ENABLED", "true")
    from modulo.settings import get_settings

    get_settings.cache_clear()


def _read_router(output_json: str) -> Callable[..., str]:
    def _read(path: str, format: str = "text", **kwargs: Any) -> str:
        if str(path).endswith("output.json"):
            return output_json
        return ""

    return _read


def _script_sandbox_mock(*, output_json: str = '{"result": "ok"}') -> MagicMock:
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


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": str(uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")),
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _workspace_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "mode": "script",
        "script_command": "python3 /home/user/main.py",
        "agent_id": _AGENT_ID,
        "env_vars": {"GH_TOKEN": "node-token"},
        "workspace_inputs": [
            {
                "url": "https://github.com/o/r.git",
                "dest": "/home/user/repo",
                "ref": {"kind": "branch", "value": "main"},
            }
        ],
    }
    node_def.update(overrides)
    return node_def


def _resolved_inputs() -> list[ResolvedInput]:
    return [
        ResolvedInput(
            url="https://github.com/o/r.git",
            dest="/home/user/repo",
            resolved_sha="a" * 40,
        ),
    ]


def _drift_results() -> list[DriftResult]:
    return [
        DriftResult(
            dest="/home/user/repo",
            expected_sha="a" * 40,
            final_sha="a" * 40,
            drift_detected=False,
        )
    ]


async def test_workspace_inputs_resolved_provisioned_and_drift_checked() -> None:
    """Happy path: host-side resolution, in-sandbox provisioning, and post-agent
    drift detection all run, and the drift results land on the result envelope."""
    fn = make_sandbox_agent_fn(_workspace_node_def())
    sandbox = _script_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.resolve_managed_inputs_host_side",
            new=AsyncMock(return_value=_resolved_inputs()),
        ) as resolve_mock,
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.provision_workspace_inputs_in_sandbox",
            new=AsyncMock(),
        ) as provision_mock,
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.detect_workspace_input_drift",
            new=AsyncMock(return_value=_drift_results()),
        ) as drift_mock,
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    resolve_mock.assert_awaited_once()
    provision_mock.assert_awaited_once()
    drift_mock.assert_awaited_once()

    drift = result["output"]["workspace_drift"]
    assert isinstance(drift, list)
    assert len(drift) == 1
    assert drift[0]["dest"] == "/home/user/repo"
    assert drift[0]["drift_detected"] is False
    assert result["output"]["workspace_drift_detected"] is False


async def test_workspace_resolution_failure_raises_sandbox_node_failed() -> None:
    """A host-side resolution failure must raise SandboxNodeFailedError and NEVER
    create a sandbox (pre-claim, re-dispatch safe)."""
    fn = make_sandbox_agent_fn(_workspace_node_def())
    sandbox = _script_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.resolve_managed_inputs_host_side",
            new=AsyncMock(
                side_effect=ProvisioningError(
                    "ref not found",
                    error_code="sandbox.input_resolution_failed",
                    retryable=True,
                )
            ),
        ),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    create_mock.assert_not_called()


async def test_workspace_provisioning_failure_raises_sandbox_node_failed() -> None:
    """A provisioning failure must raise SandboxNodeFailedError; the agent command
    must never run on partial provision."""
    fn = make_sandbox_agent_fn(_workspace_node_def())
    sandbox = _script_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.resolve_managed_inputs_host_side",
            new=AsyncMock(return_value=_resolved_inputs()),
        ),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.provision_workspace_inputs_in_sandbox",
            new=AsyncMock(
                side_effect=ProvisioningError(
                    "clone failed",
                    error_code="sandbox.input_provision_failed",
                    retryable=True,
                )
            ),
        ),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())


async def test_workspace_drift_detection_error_is_best_effort() -> None:
    """A drift-detection failure must NOT fail the node — it is logged and the
    audit write still occurs (best-effort, FAR-800 except/Exception branch)."""
    fn = make_sandbox_agent_fn(_workspace_node_def())
    sandbox = _script_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.resolve_managed_inputs_host_side",
            new=AsyncMock(return_value=_resolved_inputs()),
        ),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.provision_workspace_inputs_in_sandbox",
            new=AsyncMock(),
        ),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.detect_workspace_input_drift",
            new=AsyncMock(side_effect=RuntimeError("drift detect boom")),
        ),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"


async def test_workspace_drift_detected_sets_run_flag() -> None:
    """When drift is detected the first-class run flag is True on the envelope."""
    fn = make_sandbox_agent_fn(_workspace_node_def())
    sandbox = _script_sandbox_mock()
    drifted = [
        DriftResult(
            dest="/home/user/repo",
            expected_sha="a" * 40,
            final_sha="b" * 40,
            drift_detected=True,
        )
    ]

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.resolve_managed_inputs_host_side",
            new=AsyncMock(return_value=_resolved_inputs()),
        ),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.provision_workspace_inputs_in_sandbox",
            new=AsyncMock(),
        ),
        patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration.detect_workspace_input_drift",
            new=AsyncMock(return_value=drifted),
        ),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert result["output"]["workspace_drift_detected"] is True
