"""FAR-1051: dispatch provider seams resolve from the PROFILE's provider_type.

Every provider seam in ``node_runner`` historically resolved the E2B provider
from the E2B API key alone, so a ``kubernetes``-bound profile could never
reach the Kubernetes provider even when it was registered. These tests pin
the new resolution contract:

1. ``provider_type`` unset / ``"e2b"`` keeps the EXACT legacy behaviour
   (key-based E2B resolution, the same refusal messages);
2. any other declared type resolves through the hub — so a ``kubernetes``
   profile reaches ``KubernetesRuntimeProvider``;
3. an UNREGISTERED declared type raises the typed
   ``ProviderNotConfiguredError`` carrying its remediation env var — with an
   E2B key present, which is exactly the case a silent fallback would hide;
4. the failure is never downgraded to a generic error and never resolved by
   falling back to another provider.

No live cluster is contacted: the Kubernetes client is lazy, and the routing
tests substitute the provider seam directly.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    SandboxTierRefusedError,
    _apply_isolation_via_provider,
    _build_dispatch_provider,
    _file_io_provider_for,
    _get_info_via_provider,
    _is_legacy_e2b_route,
    _read_file_via_provider,
    _read_log_tail_via_provider,
    _write_file_via_provider,
)
from modulo.core.runtime_provider import (
    ExecResult,
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
    ProviderNotConfiguredError,
    RuntimeProvider,
    WorkspaceSpec,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))


class _RecordingProvider(RuntimeProvider):
    """Minimal provider that records every ABC call it receives."""

    provider_id = "kubernetes"
    provider_aliases = frozenset({"k8s"})

    def __init__(self) -> None:
        self.isolation_calls: list[tuple[str, IsolationPolicy]] = []
        self.write_calls: list[tuple[str, str]] = []

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "modulo-ws-recording"

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
        self.isolation_calls.append((provider_ref, policy))
        return "installed" if policy.single_pr_per_run else None

    async def write_file(self, provider_ref: str, path: str, data: bytes) -> None:
        self.write_calls.append((provider_ref, path))


def _patch_profile_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> AsyncMock:
    """Route the hub-resolved seam (``_build_profile_provider``) to a fake."""
    builder = AsyncMock(return_value=provider)
    monkeypatch.setattr(nr, "_build_profile_provider", builder)
    return builder


def _forbid_legacy_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if the E2B key-based seam is used for a non-E2B profile."""

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the legacy E2B key-based builder must not run for a profile-declared provider type")

    monkeypatch.setattr(nr, "_build_isolation_provider", _boom)
    monkeypatch.setattr(nr, "_build_file_io_provider", _boom)


def _isolation_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "org_id": _ORG_ID,
        "run_id": "run-1",
        "read_only": True,
        "git_credentials": None,
        "egress_policy": None,
        "egress_allowlist": None,
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Route classification
# ---------------------------------------------------------------------------


def test_route_classification_keeps_the_legacy_e2b_default() -> None:
    """``None`` (profile-less dispatch) and ``"e2b"`` are the legacy route;
    any other declared type takes the hub-resolved arm."""
    assert _is_legacy_e2b_route(None) is True
    assert _is_legacy_e2b_route("") is True
    assert _is_legacy_e2b_route("e2b") is True
    assert _is_legacy_e2b_route("kubernetes") is False


# ---------------------------------------------------------------------------
# _build_dispatch_provider
# ---------------------------------------------------------------------------


async def test_build_dispatch_provider_resolves_kubernetes_through_the_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered Kubernetes provider is what create_workspace runs against."""
    monkeypatch.setenv("MODULO_KUBERNETES_ENABLED", "1")
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")

    provider = await _build_dispatch_provider("kubernetes")

    assert provider is not None
    assert provider.provider_id == "kubernetes"


async def test_build_dispatch_provider_refuses_unregistered_kubernetes_with_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silent fallback: with an E2B key sitting right there, an
    unregistered ``kubernetes`` profile still fails as the typed
    ``ProviderNotConfiguredError`` naming the env var that would register it —
    it never resolves the E2B provider instead."""
    monkeypatch.delenv("MODULO_KUBERNETES_ENABLED", raising=False)
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
    monkeypatch.setenv("E2B_API_KEY", "test-key")

    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        await _build_dispatch_provider("kubernetes")

    assert excinfo.value.provider_type == "kubernetes"
    assert excinfo.value.env_var == "MODULO_KUBERNETES_ENABLED"


async def test_build_dispatch_provider_legacy_route_without_a_key_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy arm is untouched: no key -> ``None``, so the caller fails
    closed with its own typed refusal (never a silent other-provider build)."""
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setattr("modulo.core.runtime_config.key_bridge.override_or", lambda *_a, **_k: None)

    assert await _build_dispatch_provider() is None
    assert await _build_dispatch_provider("e2b") is None


async def test_build_dispatch_provider_legacy_route_with_a_key_returns_e2b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy arm resolves the E2B provider exactly as before."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")

    provider = await _build_dispatch_provider("e2b")

    assert provider is not None
    assert provider.provider_id == "e2b"


# ---------------------------------------------------------------------------
# _apply_isolation_via_provider
# ---------------------------------------------------------------------------


async def test_isolation_routes_to_the_profile_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The policy step addresses the profile's provider, not a fresh E2B one."""
    provider = _RecordingProvider()
    builder = _patch_profile_provider(monkeypatch, provider)
    _forbid_legacy_builder(monkeypatch)

    status = await _apply_isolation_via_provider(
        "modulo-ws-1",
        provider_type="kubernetes",
        **_isolation_kwargs(single_pr_per_run=True, guard_owner="n1"),
    )

    builder.assert_awaited_once_with("kubernetes")
    assert status == "installed"
    assert provider.isolation_calls
    ref, policy = provider.isolation_calls[0]
    assert ref == "modulo-ws-1"
    assert policy.read_only is True
    assert policy.guard_owner == "n1"


async def test_isolation_refuses_unregistered_profile_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcement-critical: an unregistered profile provider raises the
    typed config error, never a tier refusal that names the WRONG env var."""
    monkeypatch.delenv("MODULO_KUBERNETES_ENABLED", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "test-key")

    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        await _apply_isolation_via_provider(
            "modulo-ws-1",
            provider_type="kubernetes",
            **_isolation_kwargs(),
        )

    assert excinfo.value.env_var == "MODULO_KUBERNETES_ENABLED"


async def test_isolation_legacy_route_keeps_the_e2b_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the profile-less route still fails closed with the SAME
    E2B remediation message it has always raised."""
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    builder = AsyncMock(side_effect=AssertionError("must not build a provider without a key"))
    monkeypatch.setattr(nr, "_build_isolation_provider", builder)

    with pytest.raises(SandboxTierRefusedError, match="MODULO_E2B_API_KEY"):
        await _apply_isolation_via_provider("modulo-ws-1", **_isolation_kwargs())
    builder.assert_not_awaited()


async def test_isolation_capability_refusal_still_maps_to_the_terminal_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 040: a provider that cannot enforce a requested control stays a
    terminal, named refusal — unchanged for a non-E2B provider."""

    class _Refusing(_RecordingProvider):
        async def apply_isolation(
            self,
            provider_ref: str,
            spec: WorkspaceSpec,
            policy: IsolationPolicy,
        ) -> str | None:
            raise ProviderCapabilityUnsupportedError("the Kubernetes tier cannot enforce this control")

    _patch_profile_provider(monkeypatch, _Refusing())

    with pytest.raises(SandboxTierRefusedError, match="Kubernetes tier cannot enforce"):
        await _apply_isolation_via_provider(
            "modulo-ws-1",
            provider_type="kubernetes",
            **_isolation_kwargs(),
        )


# ---------------------------------------------------------------------------
# File I/O + log-tail seams
# ---------------------------------------------------------------------------


async def test_file_io_routes_to_the_profile_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Context files are written through the profile's provider."""
    provider = _RecordingProvider()
    builder = _patch_profile_provider(monkeypatch, provider)
    _forbid_legacy_builder(monkeypatch)

    resolved, ref = await _file_io_provider_for("modulo-ws-1", provider_type="kubernetes")
    await _write_file_via_provider("modulo-ws-1", "/home/user/prompt.md", "hello", provider_type="kubernetes")

    builder.assert_awaited_with("kubernetes")
    assert resolved is provider
    assert ref == "modulo-ws-1"
    assert provider.write_calls
    assert provider.write_calls[0] == ("modulo-ws-1", "/home/user/prompt.md")


async def test_file_io_refuses_unregistered_profile_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No E2B key can rescue a Kubernetes profile's file I/O."""
    monkeypatch.delenv("MODULO_KUBERNETES_ENABLED", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "test-key")

    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        await _file_io_provider_for("modulo-ws-1", provider_type="kubernetes")

    assert excinfo.value.env_var == "MODULO_KUBERNETES_ENABLED"


async def test_log_tail_reads_through_the_profile_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The diagnostics probe reaches the profile's provider too."""
    provider = _RecordingProvider()
    provider.read_log_tail = AsyncMock(return_value=b"pod tail")  # type: ignore[method-assign]
    _patch_profile_provider(monkeypatch, provider)

    tail = await _read_log_tail_via_provider("modulo-ws-1", provider_type="kubernetes")

    assert tail == "pod tail"
    provider.read_log_tail.assert_awaited_once_with("modulo-ws-1", max_bytes=6000)


async def test_log_tail_stays_never_raising_for_a_profile_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe contract is fail-open by design on BOTH arms: a provider
    failure degrades to an empty tail, never a raised error."""
    provider = _RecordingProvider()
    provider.read_log_tail = AsyncMock(side_effect=RuntimeError("api server down"))  # type: ignore[method-assign]
    _patch_profile_provider(monkeypatch, provider)

    tail = await _read_log_tail_via_provider("modulo-ws-1", provider_type="kubernetes")

    assert not tail


# ---------------------------------------------------------------------------
# Client lifecycle (FAR-1051 follow-up): build-and-close vs borrow
# ---------------------------------------------------------------------------


class _CloseTrackingProvider(_RecordingProvider):
    """A provider that counts its ``close()`` calls (lifecycle assertions)."""

    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1

    async def get_info(self, provider_ref: str, path: str) -> Any:
        from modulo.core.runtime_provider import WorkspaceFileInfo

        return WorkspaceFileInfo(path=path, size=1, is_dir=False)


async def test_seams_close_a_provider_they_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1051: a profile-typed seam that BUILDS its own provider disposes it
    before returning — a per-call hub must never leak a client (the previous
    behaviour: five seam invocations, zero closes)."""
    provider = _CloseTrackingProvider()
    provider.read_log_tail = AsyncMock(return_value=b"pod tail")  # type: ignore[method-assign]
    _patch_profile_provider(monkeypatch, provider)
    _forbid_legacy_builder(monkeypatch)

    await _write_file_via_provider("modulo-ws-1", "/home/user/prompt.md", "hi", provider_type="kubernetes")
    await _read_file_via_provider("modulo-ws-1", "/home/user/prompt.md", provider_type="kubernetes")
    await _get_info_via_provider("modulo-ws-1", "/home/user/prompt.md", provider_type="kubernetes")
    tail = await _read_log_tail_via_provider("modulo-ws-1", provider_type="kubernetes")
    status = await _apply_isolation_via_provider(
        "modulo-ws-1",
        provider_type="kubernetes",
        **_isolation_kwargs(),
    )

    assert tail == "pod tail"
    assert status is None
    assert provider.close_calls == 5


async def test_seams_borrow_the_dispatch_provider_and_never_close_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-1051: when the dispatch's own already-resolved provider is threaded
    in, the seam REUSES it — no hub build — and leaves disposal to the
    dispatch's finally (closing it here would kill the live workspace)."""
    builder = AsyncMock(side_effect=AssertionError("a borrowed provider must not trigger a hub build"))
    monkeypatch.setattr(nr, "_build_profile_provider", builder)
    borrowed = _CloseTrackingProvider()
    borrowed.read_log_tail = AsyncMock(return_value=b"pod tail")  # type: ignore[method-assign]

    await _write_file_via_provider(
        "modulo-ws-1",
        "/home/user/prompt.md",
        "hi",
        provider_type="kubernetes",
        borrowed_provider=borrowed,
    )
    tail = await _read_log_tail_via_provider(
        "modulo-ws-1",
        provider_type="kubernetes",
        borrowed_provider=borrowed,
    )
    resolved, ref = await _file_io_provider_for(
        "modulo-ws-1",
        provider_type="kubernetes",
        borrowed_provider=borrowed,
    )
    status = await _apply_isolation_via_provider(
        "modulo-ws-1",
        provider_type="kubernetes",
        borrowed_provider=borrowed,
        **_isolation_kwargs(),
    )

    builder.assert_not_awaited()
    assert resolved is borrowed
    assert ref == "modulo-ws-1"
    assert tail == "pod tail"
    assert status is None
    assert borrowed.close_calls == 0


async def test_legacy_e2b_arm_keeps_its_unclosed_per_call_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour pin: the legacy (profile-less / ``e2b``) route is unchanged —
    its key-based per-call provider is still built per call and NOT closed
    here, exactly as before the FAR-1051 follow-up."""
    provider = _CloseTrackingProvider()
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
    monkeypatch.setattr(nr, "_build_file_io_provider", AsyncMock(return_value=provider))

    await _write_file_via_provider("sbx-1", "/home/user/prompt.md", "hi")

    assert provider.close_calls == 0


# ---------------------------------------------------------------------------
# Collection arms (Branch Fixer coverage): seam disposal + collector errors
# ---------------------------------------------------------------------------


async def test_close_seam_provider_none_is_a_noop() -> None:
    await nr._close_seam_provider(None)


async def test_close_seam_provider_swallows_failures_and_propagates_cancellation() -> None:
    """A best-effort close failure is logged, never raised; cancellation (which
    is already unwinding) propagates."""

    class _FailingClose:
        async def close(self) -> None:
            raise RuntimeError("close failed")

    await nr._close_seam_provider(cast(Any, _FailingClose()))

    class _CancellingClose:
        async def close(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await nr._close_seam_provider(cast(Any, _CancellingClose()))


async def test_log_tail_cancellation_from_a_borrowed_provider_propagates() -> None:
    """``CancelledError`` from a borrowed provider is never swallowed into an
    empty tail — it re-raises (the probe's fail-open contract is only for
    real failures)."""
    provider = _RecordingProvider()
    provider.read_log_tail = AsyncMock(side_effect=asyncio.CancelledError)  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await _read_log_tail_via_provider("modulo-ws-1", provider_type="kubernetes", borrowed_provider=provider)
