"""Tests for FAR-802 workspace-inputs deferrals: killswitch, run-detail surfacing, daily-facts count.

Covers:
  - Killswitch OFF (default) => Settings field defaults to False.
  - Killswitch error code and ProvisioningError construction.
  - Run-detail surfacing: ``GET /runs/{id}`` returns audit records with no
    secret fields; absent when no inputs.
  - Daily-facts ``workspace_inputs_count`` field exists on model.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import make_sandbox_agent_fn
from modulo.core.pipeline_engine.workspace_input_orchestration import ResolvedInput

# ---------------------------------------------------------------------------
# Killswitch tests
# ---------------------------------------------------------------------------


class TestWorkspaceInputsKillswitch:
    """FAR-802 killswitch: default OFF short-circuits with clear error code."""

    def test_default_settings_killswitch_is_off(self):
        """Settings.modulo_workspace_inputs_enabled defaults to False."""
        from modulo.settings import Settings

        s = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
        )
        assert s.modulo_workspace_inputs_enabled is False

    def test_killswitch_can_be_enabled(self):
        """Settings.modulo_workspace_inputs_enabled can be set to True."""
        from modulo.settings import Settings

        s = Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_workspace_inputs_enabled=True,
        )
        assert s.modulo_workspace_inputs_enabled is True

    def test_disabled_error_code_matches_registry(self):
        """The sandbox.workspace_inputs_disabled error code is in the registry."""
        from modulo.core.pipeline_engine.error_codes import (
            _CODE_SANDBOX_WORKSPACE_INPUTS_DISABLED,
            ERROR_CODE_REGISTRY,
        )

        assert _CODE_SANDBOX_WORKSPACE_INPUTS_DISABLED == "sandbox.workspace_inputs_disabled"
        assert "sandbox.workspace_inputs_disabled" in ERROR_CODE_REGISTRY

    def test_provisioning_error_carries_disabled_code(self):
        """ProvisioningError constructed with the disabled code carries it correctly."""
        from modulo.core.pipeline_engine.workspace_input_orchestration import (
            ProvisioningError,
        )

        exc = ProvisioningError(
            "Managed workspace inputs are disabled (MODULO_WORKSPACE_INPUTS_ENABLED is not set or false)",
            error_code="sandbox.workspace_inputs_disabled",
            retryable=False,
        )
        assert exc.error_code == "sandbox.workspace_inputs_disabled"
        assert exc.retryable is False
        assert "disabled" in str(exc)

    def test_error_code_imported_in_node_runner(self):
        """_CODE_SANDBOX_WORKSPACE_INPUTS_DISABLED is importable in node_runner."""
        from modulo.core.pipeline_engine import node_runner

        assert hasattr(node_runner, "_CODE_SANDBOX_WORKSPACE_INPUTS_DISABLED")


# ---------------------------------------------------------------------------
# Run-detail surfacing tests
# ---------------------------------------------------------------------------


class TestRunDetailWorkspaceInputsSurfacing:
    """FAR-802: GET /runs/{id} surfaces workspace input audit records."""

    def test_run_response_has_workspace_inputs_field(self):
        """RunResponse model accepts workspace_inputs parameter."""
        from modulo.api.routes.runs import RunResponse

        resp = RunResponse(
            run_id=uuid.uuid4(),
            status="complete",
            pipeline_id=uuid.uuid4(),
            langgraph_thread_id="test-thread",
            workspace_inputs=[
                {
                    "input_name": "repo",
                    "host": "github.com",
                    "url_redacted": "https://github.com/org/repo.git",
                    "resolved_sha": "abc123",
                    "status": "provisioned",
                }
            ],
        )
        assert resp.workspace_inputs is not None
        assert len(resp.workspace_inputs) == 1
        assert resp.workspace_inputs[0]["input_name"] == "repo"

    def test_run_response_workspace_inputs_none_when_absent(self):
        """RunResponse defaults workspace_inputs to None."""
        from modulo.api.routes.runs import RunResponse

        resp = RunResponse(
            run_id=uuid.uuid4(),
            status="complete",
            pipeline_id=uuid.uuid4(),
            langgraph_thread_id="test-thread",
        )
        assert resp.workspace_inputs is None

    def test_audit_record_has_no_secret_fields(self):
        """WorkspaceInputAuditRecord.to_audit_dict never contains credentials."""
        from modulo.core.pipeline_engine.workspace_input_audit import (
            WorkspaceInputAuditRecord,
        )

        record = WorkspaceInputAuditRecord(
            input_name="repo",
            connector_instance_id=None,
            host="github.com",
            url_redacted="https://github.com/org/repo.git",
            requested_ref_kind="branch",
            requested_ref_value="main",
            resolved_sha="abc123def456abc123def456abc123def456abc1",
            final_sha="abc123def456abc123def456abc123def456abc1",
            drift_detected=False,
            dest="/home/user/repo",
            status="provisioned",
        )
        d = record.to_audit_dict()
        # Must not contain any secret-like keys.
        secret_keys = {"password", "token", "secret", "credential", "api_key", "private_key"}
        assert secret_keys.isdisjoint(d.keys())
        # URL must be redacted (no userinfo).
        assert "@" not in d["url_redacted"]

    def test_audit_record_strips_userinfo_from_url(self):
        """URL redaction strips user:pass@ from URLs."""
        from modulo.core.pipeline_engine.workspace_input_audit import (
            WorkspaceInputAuditRecord,
        )

        record = WorkspaceInputAuditRecord(
            input_name="repo",
            connector_instance_id=None,
            host="github.com",
            url_redacted="https://user:pass@github.com/org/repo.git",
            requested_ref_kind="branch",
            requested_ref_value="main",
            resolved_sha="abc123def456abc123def456abc123def456abc1",
            final_sha=None,
            drift_detected=False,
            dest="/home/user/repo",
            status="resolved",
        )
        d = record.to_audit_dict()
        assert "user" not in d["url_redacted"]
        assert "pass" not in d["url_redacted"]
        assert "github.com" in d["url_redacted"]

    def test_do_get_workspace_inputs_exists(self):
        """_do_get_workspace_inputs helper exists and is async."""
        import inspect

        from modulo.api.routes.runs import _do_get_workspace_inputs

        assert inspect.iscoroutinefunction(_do_get_workspace_inputs)


# ---------------------------------------------------------------------------
# Daily-facts workspace_inputs_count tests
# ---------------------------------------------------------------------------


class TestDailyFactsWorkspaceInputsCount:
    """FAR-802: RunDailyFact.workspace_inputs_count field."""

    def test_model_has_workspace_inputs_count_column(self):
        """RunDailyFact model includes workspace_inputs_count column."""
        from modulo.db.models.run_daily_facts import RunDailyFact

        col = RunDailyFact.__table__.c.workspace_inputs_count
        assert col is not None
        assert col.nullable is True

    def test_fact_workspace_inputs_count_exists(self):
        """_fact_workspace_inputs_count helper exists and is async."""
        import inspect

        from modulo.core.analytics import _fact_workspace_inputs_count

        assert inspect.iscoroutinefunction(_fact_workspace_inputs_count)

    def test_fact_workspace_inputs_count_signature(self):
        """_fact_workspace_inputs_count takes session and run parameters."""
        import inspect

        from modulo.core.analytics import _fact_workspace_inputs_count

        sig = inspect.signature(_fact_workspace_inputs_count)
        params = list(sig.parameters.keys())
        assert "session" in params
        assert "run" in params


# ---------------------------------------------------------------------------
# Killswitch behavioral dispatch tests (prove-the-fix)
# ---------------------------------------------------------------------------

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_ID = str(uuid.uuid4())


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


@pytest.fixture(autouse=True)
def _remote_e2b_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Script mode requires a remote E2B provider (same seam as the bindings tests)."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-e2b-key")


class TestWorkspaceInputsKillswitchDispatch:
    """Behavioral prove-the-fix for the FAR-802 killswitch gate in
    ``_sandbox_agent_impl``: when MWI is disabled the gate MUST fire before any
    sandbox is created and short-circuit the node; when enabled it MUST NOT fire
    and the resolution/provisioning path proceeds."""

    async def test_disabled_blocks_provisioning_no_sandbox_created(self) -> None:
        """MWI OFF (default): dispatch fires the killswitch before any sandbox is
        created — the node fails (never 'completed') with the dedicated
        workspace-inputs-disabled message, and AsyncSandbox.create is NEVER
        called."""
        from modulo.settings import get_settings

        # Default is OFF; assert the precondition explicitly so the test is
        # meaningful even if the default ever flips.
        assert get_settings().modulo_workspace_inputs_enabled is False

        fn = make_sandbox_agent_fn(_workspace_node_def())
        sandbox = _script_sandbox_mock()

        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
            patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        ):
            result = await fn(_run_state())

        # Gate fired: dispatch short-circuited with the disabled error code and
        # never created a sandbox (pre-claim, re-dispatch safe).
        create_mock.assert_not_called()
        assert result["output"]["status"] == "failed"
        assert "disabled" in (result["output"].get("error_message") or "").lower()

    async def test_enabled_passes_gate_and_resolves_inputs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MWI ON: the gate does not fire — host-side resolution runs and a sandbox
        is created (the dispatch proceeds past the killswitch)."""
        from modulo.settings import get_settings

        monkeypatch.setattr(get_settings(), "modulo_workspace_inputs_enabled", True)

        fn = make_sandbox_agent_fn(_workspace_node_def())
        sandbox = _script_sandbox_mock()

        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
            patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration.resolve_managed_inputs_host_side",
                new=AsyncMock(return_value=_resolved_inputs()),
            ) as resolve_mock,
        ):
            result = await fn(_run_state())

        # Gate not tripped: resolution ran and the sandbox was created.
        resolve_mock.assert_awaited_once()
        create_mock.assert_awaited_once()
        assert result["output"]["status"] == "completed"
