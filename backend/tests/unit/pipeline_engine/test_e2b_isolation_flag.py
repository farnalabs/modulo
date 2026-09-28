"""FAR-1050 R3/R6: sandbox-policy invocation (site T7) through the ABC.

Drives the T7 call site inside ``_sandbox_agent_impl`` through the real
dispatch (sandbox mock, no network) and proves:

1. The call routes through ``provider.apply_isolation`` with the resolved
   policy, and the legacy engine-side ``apply_sandbox_policy`` is never
   invoked (R6 deleted the legacy branch and the ``MODULO_E2B_VIA_PROVIDER``
   flag, so the provider routing is unconditional rather than flag-gated).
2. The ``_should_apply_sandbox_policy`` predicate is unchanged: a node with
   no policy still never invokes the provider at all.
3. Refusal maps to a TERMINAL named code: a provider's typed
   ``ProviderCapabilityUnsupportedError`` surfaces as
   ``SandboxTierRefusedError`` (named code ``sandbox.tier_refused``,
   never-retryable) with the typed cause preserved.
4. ``_apply_isolation_via_provider`` fail-closed units: no key, provider
   unavailable, missing sandbox id, cancellation, and the spec/policy
   construction (nil-UUID fallbacks for a session-factory-less dispatch).

A21 (node_runner must not import or call ``apply_sandbox_policy``) is now
enforced structurally by
``backend/tests/architecture/test_e2b_bound_form_guards.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import node_runner as node_runner_module
from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    SandboxTierRefusedError,
    _apply_isolation_via_provider,
    _build_isolation_provider,
    _should_apply_sandbox_policy,
    make_sandbox_agent_fn,
)
from modulo.core.pipeline_engine.sandbox_policy import DELIVERY_SENTINEL_SPEC_KEY
from modulo.core.runtime_provider import (
    ExecResult,
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    WorkspaceSpec,
)
from tests.unit.pipeline_engine.conftest import install_fake_dispatch

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_FIXED_ALLOWLIST: list[dict[str, Any]] = [{"host": "api.example.com", "port": 443}]


# ---------------------------------------------------------------------------
# Fakes / harness (mirrors test_e2b_via_provider_flag's no-output scenario)
# ---------------------------------------------------------------------------


class _RecordingIsolationProvider(RuntimeProvider):
    """Records ``apply_isolation`` calls (the ABC path)."""

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
    legacy = AsyncMock(
        side_effect=AssertionError("legacy apply_sandbox_policy must not run when routed via the provider")
    )
    monkeypatch.setattr("modulo.core.pipeline_engine.sandbox_policy.apply_sandbox_policy", legacy)
    return legacy


# ---------------------------------------------------------------------------
# Call-site routing: the T7 site runs through the provider primitive
# ---------------------------------------------------------------------------


async def test_isolation_routes_through_provider_apply_isolation(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    builder = _patch_isolation_builder(monkeypatch, fake)
    legacy = _legacy_must_not_run(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def())
    sandbox = await _completed_no_output_sandbox("sbx-iso")
    # FAR-1050 R4: the provider path provisions through the dispatch seam, so the
    # workspace ref the isolation primitive is addressed with comes from it.
    install_fake_dispatch(monkeypatch, ref="sbx-iso")
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
    # the legacy invocation receives on the legacy path).
    assert ref == "sbx-iso"
    assert isinstance(spec, WorkspaceSpec)
    assert spec.organisation_id == uuid.UUID(_ORG_ID)
    assert isinstance(policy, IsolationPolicy)
    assert policy.read_only is True
    assert policy.command_timeout == 60.0
    # FAR-1050 R2b: the provider dispatch also routed its file writes through
    # the ABC primitive (the fake provider), never the legacy handle.
    assert any(e.startswith("write:") for e in fake_file_io.events)
    assert not sandbox.files.write.called


async def test_provider_predicate_still_gates_no_policy_no_invocation(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The `_should_apply_sandbox_policy` predicate is unchanged: a node
    with no isolation controls triggers NEITHER path, even the provider path."""
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
# Refusal -> terminal named code (typed error catchability)
# ---------------------------------------------------------------------------


async def test_provider_capability_refusal_maps_to_terminal_named_code(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """ADR 040: apply_isolation's refusal is a TERMINAL run failure with a
    named error code — never a retry-loop. The typed cause is preserved."""
    from modulo.core.pipeline_engine import runtime_retry
    from modulo.core.pipeline_engine.error_codes import LEGACY_ALIASES

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


async def test_build_isolation_provider_constructs_real_e2b_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real builder body runs (delegating to the R1 hub seam) and
    returns the E2B provider — no network, no injection.

    Restores the R1 hub seam the autouse bridge fixture replaced, so this
    exercises the REAL chain rather than the mock-sandbox bridge.
    """
    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider
    from tests.unit._e2b_sandbox_bridge import original_seam

    monkeypatch.setattr(node_runner_module, "_build_log_tail_provider", original_seam("_build_log_tail_provider"))
    provider = await _build_isolation_provider("test-key")
    assert isinstance(provider, E2BRuntimeProvider)


async def test_build_isolation_provider_returns_none_when_hub_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modulo.core.runtime_provider.hub import RuntimeProviderHub
    from tests.unit._e2b_sandbox_bridge import original_seam

    async def _boom(self: RuntimeProviderHub, config: dict[str, Any]) -> None:
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(RuntimeProviderHub, "initialise", _boom)
    monkeypatch.setattr(node_runner_module, "_build_log_tail_provider", original_seam("_build_log_tail_provider"))
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


# ---------------------------------------------------------------------------
# FAR-1264: sentinel gating for the one-PR-per-run gh guard
# ---------------------------------------------------------------------------


def test_should_apply_sandbox_policy_false_without_a_sentinel() -> None:
    """A node with no enforcement control AND no delivery_sentinel gets NO
    policy step (the pre-FAR-1264 behaviour, unchanged)."""
    assert (
        _should_apply_sandbox_policy(
            read_only=False,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
        )
        is False
    )


def test_should_apply_sandbox_policy_true_with_a_sentinel() -> None:
    """FAR-1264: a non-empty delivery_sentinel alone runs the policy step
    (Prompt-to-PR's shape: read_only/git/egress all default)."""
    assert (
        _should_apply_sandbox_policy(
            read_only=False,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
            delivery_sentinel="PR_CREATED",
        )
        is True
    )


def test_should_apply_sandbox_policy_keeps_the_existing_triggers() -> None:
    """Regression: each pre-existing trigger still fires, sentinel or not."""
    base = {"git_credentials": None, "egress_policy": None, "egress_allowlist": None}
    assert _should_apply_sandbox_policy(read_only=True, **base) is True
    assert (
        _should_apply_sandbox_policy(
            read_only=False, git_credentials="scoped", egress_policy=None, egress_allowlist=None
        )
        is True
    )
    assert (
        _should_apply_sandbox_policy(read_only=False, git_credentials="none", egress_policy=None, egress_allowlist=None)
        is True
    )
    assert (
        _should_apply_sandbox_policy(
            read_only=False,
            git_credentials=None,
            egress_policy="selected",
            egress_allowlist=[{"host": "x", "port": 443}],
        )
        is True
    )
    # selected WITHOUT an allowlist stays False (unchanged).
    assert (
        _should_apply_sandbox_policy(
            read_only=False, git_credentials=None, egress_policy="selected", egress_allowlist=None
        )
        is False
    )


async def test_sentinel_only_node_routes_isolation_with_sentinel_metadata(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """FAR-1264 end-to-end gating: a sentinel-only node (no enforcement
    control) NOW routes through ``provider.apply_isolation``, carrying the
    sentinel on the per-invocation WorkspaceSpec."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    builder = _patch_isolation_builder(monkeypatch, fake)
    legacy = _legacy_must_not_run(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, delivery_sentinel="PR_CREATED"))
    sandbox = await _completed_no_output_sandbox("sbx-sentinel")
    install_fake_dispatch(monkeypatch, ref="sbx-sentinel")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    builder.assert_awaited_once()
    legacy.assert_not_awaited()
    assert len(fake.calls) == 1
    _, spec, policy = fake.calls[0]
    assert spec.workspace_metadata.get(DELIVERY_SENTINEL_SPEC_KEY) == "PR_CREATED"
    # Sentinel-only: every ENFORCEMENT control stays default (the policy step
    # installs only the gh guard).
    assert policy.read_only is False
    assert policy.git_credentials is None


async def test_sentinel_only_isolation_failure_does_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The gh-guard install is best-effort by contract: when a sentinel-only
    node's isolation invocation cannot run (no provider), the run PROCEEDS
    exactly as it did before FAR-1264 (degrading to the prompt-level guard)
    instead of dying with a tier refusal."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    _patch_isolation_builder(monkeypatch, None)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, delivery_sentinel="PR_CREATED"))
    sandbox = await _completed_no_output_sandbox("sbx-guard-only")
    install_fake_dispatch(monkeypatch, ref="sbx-guard-only")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        # The SAME failure the no-policy baseline produces: the run reaches
        # the agent command, NOT a SandboxTierRefusedError.
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())


async def test_enforcement_node_isolation_failure_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The best-effort swallow applies ONLY to sentinel-only invocations: a
    node that asked for an enforcement control (read_only here) keeps the
    pre-existing fail-closed tier refusal."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    _patch_isolation_builder(monkeypatch, None)

    fn = make_sandbox_agent_fn(_base_node_def())
    sandbox = await _completed_no_output_sandbox("sbx-enforcement")
    install_fake_dispatch(monkeypatch, ref="sbx-enforcement")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxTierRefusedError),
    ):
        await fn(_run_state())


async def test_helper_threads_delivery_sentinel_into_spec_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_apply_isolation_via_provider`` threads the sentinel onto the
    per-invocation WorkspaceSpec (the carrier the E2B call site reads), and
    omits the key entirely when there is no sentinel."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)

    await _apply_isolation_via_provider(
        "sbx-guard",
        org_id=_ORG_ID,
        run_id="run-1",
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
        delivery_sentinel="PR_CREATED",
    )
    _, spec, _ = fake.calls[0]
    assert spec.workspace_metadata == {DELIVERY_SENTINEL_SPEC_KEY: "PR_CREATED"}

    await _apply_isolation_via_provider(
        "sbx-plain",
        org_id=_ORG_ID,
        run_id="run-1",
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
    )
    _, spec, _ = fake.calls[1]
    assert not spec.workspace_metadata
