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
import logging
import uuid
from contextlib import nullcontext
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
from modulo.core.runtime_provider import (
    ExecResult,
    ExecStreamChunk,
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    WorkspaceSpec,
)
from tests.unit.pipeline_engine.conftest import FakeDispatchProvider, install_fake_dispatch
from tests.unit.pipeline_engine.test_sandbox_policy import shim_created_pr_stdout

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_FIXED_ALLOWLIST: list[dict[str, Any]] = [{"host": "api.example.com", "port": 443}]


# ---------------------------------------------------------------------------
# Fakes / harness (mirrors test_e2b_log_tail_provider's no-output scenario)
# ---------------------------------------------------------------------------


class _RecordingIsolationProvider(RuntimeProvider):
    """Records ``apply_isolation`` calls (the ABC path)."""

    provider_id = "e2b"

    def __init__(self, install_status: str | None = "installed") -> None:
        self.calls: list[tuple[str, WorkspaceSpec, IsolationPolicy]] = []
        # FAR-1315 (MAJOR 2b): the status apply_sandbox_policy would classify
        # for a flagged node's guard install. Tests vary it to model a live
        # install ("installed") vs the shipped runner image (no gh -> "absent").
        self.install_status = install_status

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
    ) -> str | None:
        self.calls.append((provider_ref, spec, policy))
        # FAR-1315: stand in for apply_sandbox_policy's classified install
        # status — a flagged node's guard DID land here (the fake installs
        # nothing but models a successful install), so the dispatch settle
        # may treat a definitive receipt=False as "confirmed no create".
        return self.install_status if policy.single_pr_per_run else None


class _RefusingIsolationProvider(_RecordingIsolationProvider):
    """A non-conforming provider: the typed capability refusal (ADR 040)."""

    async def apply_isolation(
        self,
        provider_ref: str,
        spec: WorkspaceSpec,
        policy: IsolationPolicy,
    ) -> str | None:
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
# FAR-1273: explicit single_pr_per_run gating for the one-PR-per-run gh guard
# ---------------------------------------------------------------------------


def test_should_apply_sandbox_policy_false_without_flag_or_control() -> None:
    """A node with no enforcement control AND no single_pr_per_run flag gets
    NO policy step (the pre-FAR-1264 behaviour, unchanged)."""
    assert (
        _should_apply_sandbox_policy(
            read_only=False,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
        )
        is False
    )


def test_should_apply_sandbox_policy_true_with_the_explicit_flag() -> None:
    """FAR-1273: the explicit single_pr_per_run flag alone runs the policy
    step (Prompt-to-PR's shape: read_only/git/egress all default)."""
    assert (
        _should_apply_sandbox_policy(
            read_only=False,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
            single_pr_per_run=True,
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


async def test_sentinel_only_node_gets_no_guard_at_all(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """FAR-1273 regression: a node with a NON-EMPTY delivery_sentinel but NO
    ``single_pr_per_run`` flag gets NO policy step and NO guard - the sentinel
    keeps only its FAR-228 idempotency meaning and must never arm the
    one-PR-per-run ``gh`` guard."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    builder = _patch_isolation_builder(monkeypatch, fake)
    legacy = _legacy_must_not_run(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, delivery_sentinel="PR_CREATED"))
    sandbox = await _completed_no_output_sandbox("sbx-sentinel-only")
    install_fake_dispatch(monkeypatch, ref="sbx-sentinel-only")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    builder.assert_not_awaited()
    legacy.assert_not_awaited()
    assert not fake.calls


async def test_flagged_node_routes_isolation_with_the_typed_flag(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """FAR-1273 end-to-end gating: a flagged node (no enforcement control)
    routes through ``provider.apply_isolation`` with
    ``policy.single_pr_per_run=True`` - the TYPED carrier - and the spec
    carries NO sentinel metadata key (the old carrier is deleted)."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    builder = _patch_isolation_builder(monkeypatch, fake)
    legacy = _legacy_must_not_run(monkeypatch)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox("sbx-flagged")
    install_fake_dispatch(monkeypatch, ref="sbx-flagged")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    builder.assert_awaited_once()
    legacy.assert_not_awaited()
    assert len(fake.calls) == 1
    _, spec, policy = fake.calls[0]
    assert policy.single_pr_per_run is True
    # Flag-only: every ENFORCEMENT control stays default (the policy step
    # installs ONLY the gh guard).
    assert policy.read_only is False
    assert policy.git_credentials is None
    # The old workspace_metadata carrier is gone: the spec is attribution only.
    assert not spec.workspace_metadata


async def test_flagged_only_isolation_failure_does_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The gh-guard install is best-effort by contract: when a flag-only
    node's isolation invocation cannot run (no provider), the run PROCEEDS
    exactly as it did before FAR-1264 (degrading to the prompt-level guard)
    instead of dying with a tier refusal."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    _patch_isolation_builder(monkeypatch, None)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox("sbx-guard-only")
    install_fake_dispatch(monkeypatch, ref="sbx-guard-only")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        # The SAME failure the no-policy baseline produces: the run reaches
        # the agent command, NOT a SandboxTierRefusedError.
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())


async def test_isolation_cancellation_propagates_through_the_call_site(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """Cancellation is re-raised, never swallowed by the best-effort swallow
    (which handles ordinary failures only): a cancelled dispatch must stay
    cancelled through the FAR-1264/1273 call-site guard."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    provider = _RecordingIsolationProvider()
    provider.apply_isolation = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]
    _patch_isolation_builder(monkeypatch, provider)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox("sbx-cancel")
    install_fake_dispatch(monkeypatch, ref="sbx-cancel")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn(_run_state())


async def test_enforcement_node_isolation_failure_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The best-effort swallow applies ONLY to flag-only invocations: a
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


async def test_helper_threads_single_pr_per_run_into_the_typed_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_apply_isolation_via_provider`` threads the flag onto the typed
    ``IsolationPolicy`` (the single carrier the E2B call site reads), and the
    default (flag omitted) is ``False`` with an untouched spec."""
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
        single_pr_per_run=True,
    )
    _, spec, policy = fake.calls[0]
    assert policy.single_pr_per_run is True
    assert not spec.workspace_metadata

    await _apply_isolation_via_provider(
        "sbx-plain",
        org_id=_ORG_ID,
        run_id="run-1",
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
    )
    _, spec, policy = fake.calls[1]
    assert policy.single_pr_per_run is False
    assert not spec.workspace_metadata


# ---------------------------------------------------------------------------
# FAR-1273: the sentinel -> flag trigger move must be OBSERVABLE, never silent
# ---------------------------------------------------------------------------

_FLAG_MISSING_EVENT = "sandbox_agent.single_pr_per_run_flag_missing"


def _flag_missing_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    """Every captured record carrying the disarm-warning event name."""
    return [record for record in caplog.records if record.getMessage() == _FLAG_MISSING_EVENT]


async def test_sentinel_without_flag_emits_the_disarm_warning(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sentinel-armed node that predates the explicit flag (the stored-graph
    case: live Prompt-to-PR nodes carry ONLY ``delivery_sentinel``) must WARN
    at the dispatch decision point so the guard's disarm is observable in
    logs/metrics - node id + pipeline id - while the run FAILS OPEN (it
    proceeds unchanged, no guard, no raised error)."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, delivery_sentinel="PR_CREATED"))
    sandbox = await _completed_no_output_sandbox("sbx-flag-missing")
    install_fake_dispatch(monkeypatch, ref="sbx-flag-missing")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    records = _flag_missing_records(caplog)
    assert records
    assert records[0].node_id == "n1"
    assert records[0].pipeline_id == "pipe-1"
    # Fail-open: the dispatch behaved exactly as it does today (no policy
    # step, no guard) - the warning is the ONLY change.
    assert not fake.calls


async def test_flagged_node_emits_no_disarm_warning(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning is about the MISSING flag, not about sentinels: a node that
    carries BOTH the sentinel and ``single_pr_per_run`` (the migrated shape)
    arms the guard and must NOT warn."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, delivery_sentinel="PR_CREATED", single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox("sbx-flag-armed")
    install_fake_dispatch(monkeypatch, ref="sbx-flag-armed")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert not _flag_missing_records(caplog)
    assert len(fake.calls) == 1
    assert fake.calls[0][2].single_pr_per_run is True


# ---------------------------------------------------------------------------
# FAR-1315: guard_owner threading + the dispatch finally settling the ledger
# ---------------------------------------------------------------------------


async def test_helper_threads_guard_owner_into_the_typed_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-1315: ``guard_owner`` (the claiming node's identity) rides the SAME
    typed carrier as the flag from the call site to the provider, and its
    default stays ``None`` so every policy built without it is unchanged."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)

    await _apply_isolation_via_provider(
        "sbx-owner",
        org_id=_ORG_ID,
        run_id="run-1",
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
        single_pr_per_run=True,
        guard_owner="n7",
    )
    assert fake.calls[0][2].guard_owner == "n7"

    await _apply_isolation_via_provider(
        "sbx-owner",
        org_id=_ORG_ID,
        run_id="run-1",
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
    )
    assert fake.calls[1][2].guard_owner is None
    assert fake.calls[1][2].single_pr_per_run is False


async def test_flagged_node_threads_guard_owner_at_the_real_call_site(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
) -> None:
    """FAR-1315 CRITICAL: ``guard_owner`` is threaded at the REAL
    ``_sandbox_agent_impl`` call site, not just in the helper.

    The helper-level test above proves ``_apply_isolation_via_provider``
    forwards the argument it is GIVEN - it says nothing about the call site
    actually giving it. With ``guard_owner=node_id`` deleted from
    ``node_runner._sandbox_agent_impl``, every flagged node claims with owner
    ``\"\"``, ``acquire_run_pr_guard`` returns ``\"acquired\"`` for EVERY node
    (empty == empty), each installs a LIVE guard, and the run produces two
    PRs - while every other test stays green. This test drives the REAL
    ``make_sandbox_agent_fn(...)`` dispatch and pins the recorded policy's
    owner to THIS node's id, the analogue of the ``single_pr_per_run is True``
    assertion beside it."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox("sbx-owner-wired")
    install_fake_dispatch(monkeypatch, ref="sbx-owner-wired")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert len(fake.calls) == 1
    policy = fake.calls[0][2]
    assert policy.single_pr_per_run is True
    # The node id ("n1" in _base_node_def) - WITHOUT this the run's one-PR
    # ledger has no owner and every flagged node re-acquires the slot.
    assert policy.guard_owner == "n1"


async def test_flagged_node_settles_the_run_claim_ledger_on_finish(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
) -> None:
    """FAR-1315 wiring: a flagged node settles its run-scoped one-PR slot in
    the dispatch ``finally`` — keyed by the run id and THIS node's id as the
    owner — so the next flagged node of the run installs a pre-planted
    refusal after an observed claim, or a fresh live guard after a
    claim-less finish."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)
    recorded: list[tuple[Any, ...]] = []
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    monkeypatch.setattr(
        sandbox_policy,
        "settle_run_pr_guard",
        lambda *args, **kwargs: recorded.append(args) or "noop",
    )

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox("sbx-settle")
    install_fake_dispatch(monkeypatch, ref="sbx-settle")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert recorded, "a flagged node must settle its run claim slot in the finally"
    run_scope, owner = recorded[0][0], recorded[0][1]
    assert run_scope == "run-1"
    assert owner == "n1"
    # The captured streams ride along for the sentinel scan.
    assert len(recorded[0]) > 2


async def test_unflagged_node_never_settles_the_run_claim_ledger(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
) -> None:
    """Regression: the settle gate is the explicit flag — a node running the
    policy step for an enforcement control only must never touch the ledger
    (its own output could otherwise be mistaken for a claim sentinel)."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = _RecordingIsolationProvider()
    _patch_isolation_builder(monkeypatch, fake)
    recorded: list[tuple[Any, ...]] = []
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    monkeypatch.setattr(
        sandbox_policy,
        "settle_run_pr_guard",
        lambda *args, **kwargs: recorded.append(args) or "noop",
    )

    fn = make_sandbox_agent_fn(_base_node_def())  # read_only=True, NO flag
    sandbox = await _completed_no_output_sandbox("sbx-settle-off")
    install_fake_dispatch(monkeypatch, ref="sbx-settle-off")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert not recorded


# ---------------------------------------------------------------------------
# FAR-1315 gate hardening: the observation channel end to end (E2B tier)
# ---------------------------------------------------------------------------
#
# The tests above MONKEYPATCH settle (wiring proofs). These drive the REAL
# ``settle_run_pr_guard`` in the dispatch ``finally`` with real spend
# evidence: shim-produced stdout, a harvested receipt, and a shim-read
# transcript that must NOT spend. Fail-without-fix for the whole block: moving
# the settle out of the finally (or passing ``None`` streams) leaves every
# assertion below failing.


class _ClaimingIsolationProvider(_RecordingIsolationProvider):
    """Stands in for ``apply_sandbox_policy``'s run-ledger claim.

    The real E2B ``apply_isolation`` takes the claim for
    (scope=``spec.run_id``, owner=``policy.guard_owner``) BEFORE the node runs
    — without it the dispatch finally's settle would have no entry to
    spend/release and every ledger assertion would be vacuous."""

    async def apply_isolation(
        self,
        provider_ref: str,
        spec: WorkspaceSpec,
        policy: IsolationPolicy,
    ) -> str | None:
        status = await super().apply_isolation(provider_ref, spec, policy)
        if policy.single_pr_per_run and spec.run_id is not None:
            from modulo.core.pipeline_engine.sandbox_policy import acquire_run_pr_guard

            acquire_run_pr_guard(str(spec.run_id), policy.guard_owner)
        return status


def _override_harvest_reply(dispatch: Any, reply: str) -> None:
    """Make the dispatch's claim-receipt harvest probe answer *reply*."""
    original = dispatch.exec_command

    async def _exec(ref: str, command: list[str], *, cmd_timeout: int | None = None) -> ExecResult:
        if command and "MODULO_CLAIM_RECEIPT" in command[-1]:
            return ExecResult(exit_code=0, stdout=reply, stderr="")
        return await original(ref, command, cmd_timeout=cmd_timeout)

    dispatch.exec_command = _exec


async def _dispatch_flagged_e2b(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
    *,
    chunks: list[tuple[str, str]],
    harvest_reply: str | None = None,
    ref: str = "sbx-flag-e2e",
    output_json: str | None = None,
    install_status: str | None = "installed",
) -> str:
    """Run the REAL flagged E2B dispatch and return this run's id.

    By default the node FAILS (no output.json), exactly like the wiring tests
    — the assertion surface is the ledger the ``finally`` settles. Passing
    ``output_json`` makes the node SUCCEED with that output (an agent-authored
    ``output.json``), which is the shape the ``pr_url`` spend arm sees.
    ``chunks`` is the agent's captured stdout/stderr; ``harvest_reply``
    overrides the receipt-harvest probe (``None`` = the fake's empty reply,
    i.e. the harvest ran but yielded no token -> unavailable);
    ``install_status`` is what apply_isolation reports for the guard install
    (``"absent"`` models the shipped runner image with no ``gh``)."""
    from modulo.core.pipeline_engine.sandbox_policy import reset_run_pr_guard_claims

    reset_run_pr_guard_claims()
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    provider = _ClaimingIsolationProvider(install_status=install_status)
    _patch_isolation_builder(monkeypatch, provider)
    dispatch = install_fake_dispatch(
        monkeypatch,
        ref=ref,
        chunks=[ExecStreamChunk(stream=stream, data=data) for stream, data in chunks],
        exit_code=0 if output_json is not None else 1,
    )
    if harvest_reply is not None:
        _override_harvest_reply(dispatch, harvest_reply)
    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox(ref)
    if output_json is not None:
        # output.json is read through the R2b file-I/O seam (the fake_file_io
        # fixture), not the legacy SDK handle.
        fake_file_io.files["/home/user/output.json"] = output_json.encode("utf-8")
    state = _run_state()
    run_id = str(uuid.uuid4())
    state["_run_id"] = run_id
    expect_failure = nullcontext() if output_json is not None else pytest.raises(SandboxNodeFailedError)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        expect_failure,
    ):
        await fn(state)
    return run_id


async def test_flagged_node_observes_shim_produced_output_and_spends_the_run_claim(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
    tmp_path,
) -> None:
    """The observation channel, end to end: stdout produced by the REAL shim's
    successful create flows through the dispatch capture into the REAL settle
    in the ``finally`` — the run's claim is SPENT for every later flagged
    node. The harvest yields no token here, so this is the sentinel FALLBACK
    arm a dispatch reaches when its receipt probe is unavailable."""
    from modulo.core.pipeline_engine.sandbox_policy import acquire_run_pr_guard, reset_run_pr_guard_claims

    shim_stdout = shim_created_pr_stdout(tmp_path)
    run_id = await _dispatch_flagged_e2b(
        monkeypatch,
        fake_file_io,
        chunks=[("stdout", shim_stdout)],
    )
    try:
        assert acquire_run_pr_guard(run_id, "later-node") == "spent"
    finally:
        reset_run_pr_guard_claims()


async def test_flagged_node_spends_from_the_harvested_receipt_when_the_sentinel_was_truncated(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
) -> None:
    """MAJOR 1 at the dispatch level: the node created the PR and then emitted
    more than the 512 KB drain window, so the captured stream carries NO
    sentinel. The platform-side receipt harvest must still spend the run —
    releasing here would let the next flagged node install a live guard and
    open a SECOND PR."""
    from modulo.core.pipeline_engine.sandbox_policy import acquire_run_pr_guard, reset_run_pr_guard_claims

    run_id = await _dispatch_flagged_e2b(
        monkeypatch,
        fake_file_io,
        chunks=[("stdout", "post-create agent noise\n" * 200)],
        harvest_reply="MODULO_CLAIM_RECEIPT_PRESENT",
    )
    try:
        assert acquire_run_pr_guard(run_id, "later-node") == "spent"
    finally:
        reset_run_pr_guard_claims()


async def test_flagged_node_shim_read_transcript_never_spends_when_the_harvest_confirms_no_receipt(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
    tmp_path,
) -> None:
    """MAJOR 2 at the dispatch level: the captured stdout contains the claim
    sentinel (the agent read the shim / echoed its text), but the harvest RAN
    and confirmed no receipt — so the run must NOT be spent and the holder's
    hold must be RELEASED. Spending here is the fail-closed DoS: every later
    flagged node pre-plants and the run delivers no PR at all."""
    from modulo.core.pipeline_engine.sandbox_policy import acquire_run_pr_guard, reset_run_pr_guard_claims

    shim_stdout = shim_created_pr_stdout(tmp_path)
    run_id = await _dispatch_flagged_e2b(
        monkeypatch,
        fake_file_io,
        chunks=[("stdout", shim_stdout)],
        harvest_reply="MODULO_CLAIM_RECEIPT_ABSENT",
    )
    try:
        assert acquire_run_pr_guard(run_id, "later-node") == "acquired"
    finally:
        reset_run_pr_guard_claims()


async def test_failing_flagged_node_releases_its_run_claim_for_a_later_node(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
) -> None:
    """A FAILING flagged node must not leak its hold: with no spend evidence
    the ``finally`` releases the slot so a later flagged node of the run can
    still claim it. Fail-without-fix: moving the settle out of the ``finally``
    leaves the entry ``held`` forever and the assertion reads ``held``."""
    from modulo.core.pipeline_engine.sandbox_policy import acquire_run_pr_guard, reset_run_pr_guard_claims

    run_id = await _dispatch_flagged_e2b(monkeypatch, fake_file_io, chunks=[])
    try:
        assert acquire_run_pr_guard(run_id, "later-node") == "acquired"
    finally:
        reset_run_pr_guard_claims()


# ---------------------------------------------------------------------------
# FAR-1315 re-gate: the three findings, proven at the REAL dispatch level
# ---------------------------------------------------------------------------


async def test_pr_url_never_outranks_a_definitive_receipt_false(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
) -> None:
    """RE-GATE MAJOR 2(a) — FALSE SPEND, end to end.

    A node whose ``gh pr create`` FAILED still reports a URL-shaped ``pr_url``
    in its agent-authored ``output.json``, and the same URL appears in the
    captured stdout (so the corroboration arm is satisfied too). The LIVE
    shim's receipt harvest nevertheless confirms NO create succeeded — that
    definitive negative must WIN and release the hold.

    Fail-without-fix: the pre-fix precedence evaluated ``pr_url`` BEFORE
    ``claim_receipt is False``, so this exact dispatch marked the run SPENT and
    every later flagged node was pre-planted — the run then delivers nothing."""
    from modulo.core.pipeline_engine.sandbox_policy import acquire_run_pr_guard, reset_run_pr_guard_claims

    url = "https://github.com/org/repo/pull/42"
    run_id = await _dispatch_flagged_e2b(
        monkeypatch,
        fake_file_io,
        chunks=[("stdout", f"attempted create, reporting {url}\n")],
        harvest_reply="MODULO_CLAIM_RECEIPT_ABSENT",
        output_json=f'{{"summary":"done","pr_url":"{url}"}}',
    )
    try:
        assert acquire_run_pr_guard(run_id, "later-node") == "acquired", (
            "a definitive receipt=False from a LIVE install must release the hold, "
            "never be outranked by an agent-authored pr_url"
        )
    finally:
        reset_run_pr_guard_claims()


async def test_absent_install_leaves_the_receipt_meaningless_so_the_sentinel_still_spends(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
    tmp_path,
) -> None:
    """RE-GATE MAJOR 2(b) — FALSE RELEASE, end to end.

    The guard install reports ``absent`` (the shipped runner image's shape:
    no ``gh`` on PATH), yet a claim sentinel is in the captured transcript.
    The receipt probe runs against a path NO SHIM EVER WROTE — it answers
    ABSENT exactly like a real "no receipt" probe, so read as definitive it
    would suppress the sentinel arm and RELEASE a genuinely unguarded create.
    With the install status threaded, the receipt is UNKNOWN and the sentinel
    fallback still spends the run.

    Fail-without-fix: the pre-fix settle read ``claim_receipt is False`` as
    definitive regardless of install status, so this dispatch released the
    hold (``acquired``) instead of spending it."""
    from modulo.core.pipeline_engine.sandbox_policy import acquire_run_pr_guard, reset_run_pr_guard_claims

    shim_stdout = shim_created_pr_stdout(tmp_path)
    run_id = await _dispatch_flagged_e2b(
        monkeypatch,
        fake_file_io,
        chunks=[("stdout", shim_stdout)],
        harvest_reply="MODULO_CLAIM_RECEIPT_ABSENT",
        install_status="absent",
    )
    try:
        assert acquire_run_pr_guard(run_id, "later-node") == "spent", (
            "an absent install must leave the receipt UNKNOWN so the sentinel fallback still spends the run"
        )
    finally:
        reset_run_pr_guard_claims()


class _BlockingHarvestDispatch(FakeDispatchProvider):
    """Dispatch fake whose RECEIPT-HARVEST probe blocks until cancelled.

    Lets a test cancel the dispatch task while it sits inside the harvest —
    the exact window the re-gate's MAJOR 1 cancellation finding is about."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.harvest_started = asyncio.Event()

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        if command and "MODULO_CLAIM_RECEIPT" in command[-1]:
            self.harvest_started.set()
            await asyncio.Event().wait()  # parks until the probe task is cancelled
        return await super().exec_command(provider_ref, command, cmd_timeout=cmd_timeout)


async def test_cancel_during_the_claim_harvest_still_tears_down(
    monkeypatch: pytest.MonkeyPatch,
    fake_file_io,
) -> None:
    """RE-GATE MAJOR 1 — teardown must be UNCONDITIONAL.

    The harvest is the FIRST await in the dispatch ``finally`` and
    ``asyncio.CancelledError`` is a BaseException the ``except Exception``
    handlers do not catch. Before the fix a cancel landing in that window
    unwound the ``finally`` BEFORE the sandbox destroy, the provider close and
    the fenced dispatch-marker clear — a leaked sandbox, a stale dispatch
    marker and a stranded ledger hold. After the fix the cancellation is
    recorded, teardown runs, and the cancellation is re-raised at the end.

    Fail-without-fix: with the old ``wait_for(shield(...))`` form this test
    sees the CancelledError but ``dispatch.events`` never contains
    ``destroy_by_ref``/``close`` and the marker-clear mock is never awaited."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    provider = _ClaimingIsolationProvider()
    _patch_isolation_builder(monkeypatch, provider)
    dispatch = _BlockingHarvestDispatch(ref="sbx-cancel-harvest")
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_dispatch_provider",
        AsyncMock(return_value=dispatch),
    )
    clear_mock = AsyncMock()
    monkeypatch.setattr(node_runner_module, "_sandbox_clear_dispatch_marker", clear_mock)

    fn = make_sandbox_agent_fn(_base_node_def(read_only=False, single_pr_per_run=True))
    sandbox = await _completed_no_output_sandbox("sbx-cancel-harvest")
    state = _run_state()
    state["_run_id"] = str(uuid.uuid4())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        task = asyncio.ensure_future(fn(state))
        await asyncio.wait_for(dispatch.harvest_started.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "destroy_by_ref" in dispatch.events, (
        f"the sandbox must still be destroyed after a cancel during the harvest: {dispatch.events}"
    )
    assert "close" in dispatch.events, (
        f"the dispatch provider must still be closed after a cancel during the harvest: {dispatch.events}"
    )
    assert clear_mock.await_count >= 1, (
        "the fenced dispatch marker must still be cleared after a cancel during the harvest"
    )
