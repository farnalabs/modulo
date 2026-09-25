"""FAR-1050 R3: flag-gated sandbox-policy invocation (site T7).

Drives the T7 call site inside ``_sandbox_agent_impl`` through the real
dispatch (sandbox mock, no network) and proves:

1. Flag OFF (default): the legacy engine-side ``apply_sandbox_policy``
   runs and the isolation provider seam never does.
2. Flag ON: the call routes through ``provider.apply_isolation`` with the
   same resolved policy, and the legacy function is never invoked.
3. Refusal maps to a TERMINAL named code: a provider's typed
   ``ProviderCapabilityUnsupportedError`` surfaces as
   ``SandboxTierRefusedError`` (named code ``sandbox.tier_refused``,
   never-retryable) with the typed cause preserved.
4. **A21 must NOT activate yet**: node_runner still imports
   ``apply_sandbox_policy`` on the flag-OFF path
   (docs/design/e2b-provider-conformance-rewire.md §4).
5. ``_apply_isolation_via_provider`` fail-closed units: no key, provider
   unavailable, missing sandbox id, cancellation, and the spec/policy
   construction (nil-UUID fallbacks for a session-factory-less dispatch).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import node_runner as node_runner_module
from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    SandboxTierRefusedError,
    _apply_isolation_via_provider,
    _build_isolation_provider,
    make_sandbox_agent_fn,
)
from modulo.core.runtime_provider import (
    ExecResult,
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    WorkspaceSpec,
)
from modulo.settings import Settings, get_settings
from tests.unit.pipeline_engine.conftest import install_fake_dispatch

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_LEGACY_IMPORT = "from modulo.core.pipeline_engine.sandbox_policy import apply_sandbox_policy"
_FIXED_ALLOWLIST: list[dict[str, Any]] = [{"host": "api.example.com", "port": 443}]


# ---------------------------------------------------------------------------
# Fakes / harness (mirrors test_e2b_via_provider_flag's no-output scenario)
# ---------------------------------------------------------------------------


class _RecordingIsolationProvider(RuntimeProvider):
    """Records ``apply_isolation`` calls (the flag-ON ABC path)."""

    provider_id = "e2b"

    def __init__(self) -> None:
        self.calls: list[tuple[str, WorkspaceSpec, IsolationPolicy]] = []

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws-fake"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def apply_isolation(
        self,
        provider_ref: str,
        spec: WorkspaceSpec,
        policy: IsolationPolicy,
    ) -> None:
        self.calls.append((provider_ref, spec, policy))


class _RefusingIsolationProvider(_RecordingIsolationProvider):
    """A non-conforming provider: the typed capability refusal (ADR 040)."""

    async def apply_isolation(
        self,
        provider_ref: str,
        spec: WorkspaceSpec,
        policy: IsolationPolicy,
    ) -> None:
        raise ProviderCapabilityUnsupportedError("Runtime provider 'Refusing' does not implement apply_isolation")


def _base_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": [_AGENT_COMMAND],
        "timeout_seconds": 30,
        # Reach site T7: the predicate fires on read_only.
        "read_only": True,
    }
    node_def.update(overrides)
    return node_def


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


async def _completed_no_output_sandbox(sandbox_id: str) -> MagicMock:
    """Command completed but output.json missing -> dispatch fails (site exit)."""
    cmd_result = MagicMock()
    cmd_result.exit_code = 1
    cmd_result.stdout = ""
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.sandbox_id = sandbox_id
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(return_value="")
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", True)


def _disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", False)


def _patch_isolation_builder(monkeypatch: pytest.MonkeyPatch, provider: Any) -> AsyncMock:
    builder = AsyncMock(return_value=provider)
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_isolation_provider",
        builder,
    )
    return builder


def _patch_legacy_sandbox_policy(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    legacy = AsyncMock(return_value=None)
    monkeypatch.setattr("modulo.core.pipeline_engine.sandbox_policy.apply_sandbox_policy", legacy)
    return legacy


def _legacy_must_not_run(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    legacy = AsyncMock(side_effect=AssertionError("legacy apply_sandbox_policy must not run when flag ON"))
    monkeypatch.setattr("modulo.core.pipeline_engine.sandbox_policy.apply_sandbox_policy", legacy)
    return legacy


def _isolation_builder_must_not_run(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    builder = AsyncMock(side_effect=AssertionError("isolation provider must not build when flag OFF"))
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_isolation_provider",
        builder,
    )
    return builder


# ---------------------------------------------------------------------------
# Call-site routing: flag OFF -> legacy, flag ON -> provider primitive
# ---------------------------------------------------------------------------


async def test_flag_off_invokes_legacy_apply_sandbox_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    builder = _isolation_builder_must_not_run(monkeypatch)
    legacy = _patch_legacy_sandbox_policy(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def())
    sandbox = await _completed_no_output_sandbox("sbx-flagoff-iso")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    legacy.assert_awaited_once()
    assert legacy.await_args.args[0] is sandbox
    assert legacy.await_args.kwargs["read_only"] is True
    builder.assert_not_awaited()


async def test_flag_on_routes_through_provider_apply_isolation(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    _enable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    builder = _patch_isolation_builder(monkeypatch, fake)
    legacy = _legacy_must_not_run(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def())
    sandbox = await _completed_no_output_sandbox("sbx-flagon-iso")
    # FAR-1050 R4: flag ON provisions through the dispatch seam, so the
    # workspace ref the isolation primitive is addressed with comes from it.
    install_fake_dispatch(monkeypatch, ref="sbx-flagon-iso")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    builder.assert_awaited_once()
    legacy.assert_not_awaited()
    assert len(fake.calls) == 1
    ref, spec, policy = fake.calls[0]
    # The provider is addressed with the dispatch's sandbox id, and the
    # resolved policy carries the node's read_only control (the same values
    # the legacy invocation receives on the flag-OFF path).
    assert ref == "sbx-flagon-iso"
    assert isinstance(spec, WorkspaceSpec)
    assert spec.organisation_id == uuid.UUID(_ORG_ID)
    assert isinstance(policy, IsolationPolicy)
    assert policy.read_only is True
    assert policy.command_timeout == 60.0
    # FAR-1050 R2b: the flag-ON dispatch also routed its file writes through
    # the ABC primitive (the fake provider), never the legacy handle.
    assert any(e.startswith("write:") for e in fake_file_io.events)
    assert not sandbox.files.write.called


async def test_flag_on_predicate_still_gates_no_policy_no_invocation(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The `_should_apply_sandbox_policy` predicate is unchanged: a node
    with no isolation controls triggers NEITHER path, even flag ON."""
    _enable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    builder = _patch_isolation_builder(monkeypatch, fake)
    legacy = _patch_legacy_sandbox_policy(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False))
    sandbox = await _completed_no_output_sandbox("sbx-nopolicy")
    install_fake_dispatch(monkeypatch, ref="sbx-nopolicy")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    builder.assert_not_awaited()
    legacy.assert_not_awaited()


# ---------------------------------------------------------------------------
# A21: node_runner still imports apply_sandbox_policy on the flag-OFF path
# ---------------------------------------------------------------------------


def test_node_runner_still_imports_apply_sandbox_policy_flag_off_path() -> None:
    """Design doc §4: the A21 guard activates only at slice R6. Until the
    legacy branch is physically removed, the engine-side import must remain
    in the source — asserted here so R3 cannot silently drop it."""
    source = Path(node_runner_module.__file__).read_text(encoding="utf-8")
    assert _LEGACY_IMPORT in source
    # ...and the flag gate that makes the legacy arm the flag-OFF path.
    assert "get_settings().modulo_e2b_via_provider" in source


# ---------------------------------------------------------------------------
# Refusal -> terminal named code (typed error catchability)
# ---------------------------------------------------------------------------


async def test_flag_on_capability_refusal_maps_to_terminal_named_code(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """ADR 040: apply_isolation's refusal is a TERMINAL run failure with a
    named error code — never a retry-loop. The typed cause is preserved."""
    from modulo.core.pipeline_engine import runtime_retry
    from modulo.core.pipeline_engine.error_codes import LEGACY_ALIASES

    _enable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    _patch_isolation_builder(monkeypatch, _RefusingIsolationProvider())
    legacy = _legacy_must_not_run(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def())
    sandbox = await _completed_no_output_sandbox("sbx-refused")
    install_fake_dispatch(monkeypatch, ref="sbx-refused")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxTierRefusedError) as excinfo,
    ):
        await fn(_run_state())

    legacy.assert_not_awaited()
    # Typed catchability: the ProviderCapabilityUnsupportedError survives as
    # the cause, so a typed handler upstream still sees it.
    assert isinstance(excinfo.value.__cause__, ProviderCapabilityUnsupportedError)
    # Named code: SandboxTierRefusedError is the executor's terminal
    # tier_refused path (executor.py terminal branch keys on this isinstance).
    assert LEGACY_ALIASES["SandboxTierRefusedError"] == "sandbox.tier_refused"
    # Never retry-loop: the name is in the never-retryable registry.
    assert "SandboxTierRefusedError" in runtime_retry._NEVER_RETRYABLE_NAMES


# ---------------------------------------------------------------------------
# _apply_isolation_via_provider / _build_isolation_provider units
# ---------------------------------------------------------------------------


async def test_build_isolation_provider_constructs_real_e2b_provider() -> None:
    """The real builder body runs (delegating to the R1 hub seam) and
    returns the E2B provider — no network, no injection."""
    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

    provider = await _build_isolation_provider("test-key")
    assert isinstance(provider, E2BRuntimeProvider)


async def test_build_isolation_provider_returns_none_when_hub_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modulo.core.runtime_provider.hub import RuntimeProviderHub

    async def _boom(self: RuntimeProviderHub, config: dict[str, Any]) -> None:
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(RuntimeProviderHub, "initialise", _boom)
    assert await _build_isolation_provider("test-key") is None


async def test_helper_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    builder = AsyncMock(side_effect=AssertionError("must not build a provider without a key"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_isolation_provider", builder)

    with pytest.raises(SandboxTierRefusedError, match="MODULO_E2B_API_KEY"):
        await _apply_isolation_via_provider(
            "sbx-1",
            org_id=_ORG_ID,
            run_id="run-1",
            read_only=True,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
        )
    builder.assert_not_awaited()


async def test_helper_fails_closed_on_invalid_sandbox_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    builder = AsyncMock(side_effect=AssertionError("must not build a provider without a sandbox id"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_isolation_provider", builder)

    with pytest.raises(SandboxTierRefusedError, match="sandbox id"):
        await _apply_isolation_via_provider(
            None,
            org_id=_ORG_ID,
            run_id="run-1",
            read_only=True,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
        )
    with pytest.raises(SandboxTierRefusedError, match="sandbox id"):
        await _apply_isolation_via_provider(
            "",
            org_id=_ORG_ID,
            run_id="run-1",
            read_only=True,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
        )
    builder.assert_not_awaited()


async def test_helper_fails_closed_when_provider_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    _patch_isolation_builder(monkeypatch, None)

    with pytest.raises(SandboxTierRefusedError, match="MODULO_E2B_API_KEY"):
        await _apply_isolation_via_provider(
            "sbx-1",
            org_id=_ORG_ID,
            run_id="run-1",
            read_only=True,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
        )


async def test_helper_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """CancelledError from the provider primitive is re-raised, never
    converted to a tier refusal (cancellation is not a refusal)."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    exploding = _RecordingIsolationProvider()
    exploding.apply_isolation = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]
    _patch_isolation_builder(monkeypatch, exploding)

    with pytest.raises(asyncio.CancelledError):
        await _apply_isolation_via_provider(
            "sbx-1",
            org_id=_ORG_ID,
            run_id="run-1",
            read_only=True,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
        )


async def test_helper_builds_spec_and_policy_with_nil_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session-factory-less dispatch: unparseable org/run and absent profile
    fall back to the nil UUID / None; the policy carries the controls."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)

    await _apply_isolation_via_provider(
        "sbx-nil",
        org_id="not-a-uuid",
        run_id="run-not-a-uuid",
        read_only=True,
        git_credentials="scoped",
        egress_policy="selected",
        egress_allowlist=_FIXED_ALLOWLIST,
        allowed_hosts={"github.com": "MODULO_GIT_CRED_0"},
    )

    assert len(fake.calls) == 1
    ref, spec, policy = fake.calls[0]
    assert ref == "sbx-nil"
    assert spec.organisation_id == uuid.UUID(int=0)
    assert spec.environment_profile_id == uuid.UUID(int=0)
    assert spec.run_id is None
    assert spec.egress_policy == "selected"
    assert policy.read_only is True
    assert policy.git_credentials == "scoped"
    assert policy.egress_allowlist is not None
    assert policy.allowed_hosts == {"github.com": "MODULO_GIT_CRED_0"}


# Flag settings sanity (mirrors R1's settings tests against this flag).
def test_flag_defaults_off() -> None:
    assert Settings(_env_file=None).modulo_e2b_via_provider is False
