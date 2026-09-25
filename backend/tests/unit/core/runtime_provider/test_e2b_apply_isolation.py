"""FAR-1050 R3: ``apply_isolation`` primitive + E2B implementation.

Exercises, without a live sandbox or network:

1. The ABC's ``apply_isolation`` default raises the typed
   ``ProviderCapabilityUnsupportedError`` (error honesty — never a raw
   ``NotImplementedError``) for a provider that does not override it, and
   the refusal is catchable as the typed error.
2. **Parity**: flag-ON ``E2BRuntimeProvider.apply_isolation`` and flag-OFF
   ``apply_sandbox_policy`` emit the same script sequence, users, and
   timeouts for a fixed policy — and produce the same outcome (raise vs
   best-effort swallow) for every failing step index, pinning the
   enforcement-critical-raise vs egress-best-effort split.
3. E2B handle resolution: a tracked sandbox is used directly (no SDK
   connect); an untracked ref reconnects via ``AsyncSandbox.connect``; a
   connect failure raises ``RuntimeError``; cancellation propagates.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.pipeline_engine.sandbox_policy import apply_sandbox_policy
from modulo.core.runtime_provider import (
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    RuntimeProviderError,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

_FIXED_ALLOWLIST: list[dict[str, Any]] = [{"host": "api.example.com", "port": 443}]


def _spec() -> WorkspaceSpec:
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        egress_policy="selected",
    )


def _policy() -> IsolationPolicy:
    return IsolationPolicy(
        read_only=True,
        git_credentials="scoped",
        egress_policy="selected",
        egress_allowlist=[{"host": "api.example.com", "port": 443}],
    )


def _legacy_kwargs() -> dict[str, Any]:
    """The exact legacy ``apply_sandbox_policy`` call for the fixed policy.

    All three named controls fire, so the sequence is git-credential scope
    -> egress allowlist -> read-only seal (three steps, in that order).
    """
    return {
        "read_only": True,
        "git_credentials": "scoped",
        "egress_policy": "selected",
        "egress_allowlist": [{"host": "api.example.com", "port": 443}],
    }


class _RecordingCommands:
    """Records every ``run`` as ``(script, user, timeout)``; fails on demand.

    Mirrors the legacy ``test_sandbox_policy`` fake: a step in ``fail_on``
    raises BEFORE its script is recorded, exactly as a real in-sandbox
    failure would surface at the call site.
    """

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls: list[tuple[str, str, float]] = []
        self._fail_on = fail_on or set()
        self._count = 0

    async def run(self, script: str, *, user: str = "root", timeout: float = 60.0) -> None:  # noqa: ASYNC109 - matches the e2b SDK signature
        index = self._count
        self._count += 1
        if index in self._fail_on:
            raise RuntimeError(f"policy step failed: {index}")
        self.calls.append((script, user, timeout))


class _RecordingSandbox:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.commands = _RecordingCommands(fail_on=fail_on)


async def _legacy_outcome(fail_on: set[int] | None) -> tuple[list[tuple[str, str, float]], str | None]:
    sandbox = _RecordingSandbox(fail_on=fail_on)
    error: str | None = None
    try:
        await apply_sandbox_policy(sandbox, **_legacy_kwargs())
    except Exception as exc:
        error = type(exc).__name__
    return sandbox.commands.calls, error


async def _provider_outcome(fail_on: set[int] | None) -> tuple[list[tuple[str, str, float]], str | None]:
    sandbox = _RecordingSandbox(fail_on=fail_on)
    provider = E2BRuntimeProvider(api_key="parity-key")
    provider._sandboxes["sbx-parity"] = sandbox  # pre-seed the tracked handle
    error: str | None = None
    try:
        await provider.apply_isolation("sbx-parity", _spec(), _policy())
    except Exception as exc:
        error = type(exc).__name__
    return sandbox.commands.calls, error


# ---------------------------------------------------------------------------
# 1. ABC default (error honesty, the R1/R2/R3 carve-out pattern)
# ---------------------------------------------------------------------------


class _NoIsolationProvider(RuntimeProvider):
    """Concrete provider that does NOT override ``apply_isolation``."""

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> Any:
        raise AssertionError("exec_command must not be called in this test")

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


async def test_abc_default_apply_isolation_raises_typed_capability_refusal() -> None:
    """ADR 040 "Error honesty": typed ``ProviderCapabilityUnsupportedError``,
    not a raw ``NotImplementedError`` — and catchable as the typed error
    (``pytest.raises(ProviderCapabilityUnsupportedError)`` IS the catch)."""
    with pytest.raises(ProviderCapabilityUnsupportedError, match="apply_isolation") as excinfo:
        await _NoIsolationProvider().apply_isolation("ref", _spec(), _policy())
    assert isinstance(excinfo.value, RuntimeProviderError)
    assert not isinstance(excinfo.value, NotImplementedError)


# ---------------------------------------------------------------------------
# 2. Parity: flag-ON apply_isolation vs flag-OFF apply_sandbox_policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fail_on",
    [
        pytest.param(set(), id="all-steps-ok"),
        pytest.param({0}, id="git-step-fails"),
        pytest.param({1}, id="egress-step-fails"),
        pytest.param({2}, id="seal-step-fails"),
    ],
)
async def test_parity_script_sequence_users_and_outcome(fail_on: set[int]) -> None:
    """For a fixed policy both paths emit the SAME scripts, users and
    timeouts, and produce the SAME outcome per failing step — git/seal
    failures raise on both, an egress failure is best-effort on both."""
    legacy_calls, legacy_error = await _legacy_outcome(fail_on)
    provider_calls, provider_error = await _provider_outcome(fail_on)

    assert provider_calls == legacy_calls
    assert provider_error == legacy_error

    if not fail_on:
        # git scoped -> egress selected -> read-only seal, all as root,
        # each bounded by the default 60s command timeout.
        assert len(provider_calls) == 3
        assert "github.com" in provider_calls[0][0]
        assert "DROP" in provider_calls[1][0].upper()
        assert "chmod" in provider_calls[2][0]
        assert {user for _, user, _ in provider_calls} == {"root"}
        assert {timeout for _, _, timeout in provider_calls} == {60.0}
    elif fail_on == {1}:
        # Egress is best-effort: git ran, egress raised-and-swallowed,
        # the seal still ran afterwards (identical on both paths).
        assert legacy_error is None
        assert "chmod" in provider_calls[-1][0]
    else:
        # Enforcement-critical steps (git, seal) raise on both paths.
        assert legacy_error == "RuntimeError"
        assert provider_error == "RuntimeError"


# ---------------------------------------------------------------------------
# 3. E2B handle resolution
# ---------------------------------------------------------------------------


async def test_apply_isolation_uses_tracked_sandbox_without_connect() -> None:
    """A tracked handle (provider-created workspace) is used directly —
    ``AsyncSandbox.connect`` must never fire."""
    sandbox = _RecordingSandbox()
    provider = E2BRuntimeProvider(api_key="tracked-key")
    provider._sandboxes["sbx-tracked"] = sandbox

    with patch(
        "e2b.AsyncSandbox.connect",
        new=AsyncMock(side_effect=AssertionError("connect must not run for a tracked sandbox")),
    ):
        await provider.apply_isolation("sbx-tracked", _spec(), _policy())

    assert len(sandbox.commands.calls) == 3


async def test_apply_isolation_connects_by_ref_when_untracked() -> None:
    """The R3 shape: a fresh provider (legacy-created workspace) reconnects
    by ref, the same by-ref pattern as ``destroy_workspace_by_ref``."""
    sandbox = _RecordingSandbox()
    provider = E2BRuntimeProvider(api_key="fresh-key")
    connect = AsyncMock(return_value=sandbox)

    with patch("e2b.AsyncSandbox.connect", new=connect):
        await provider.apply_isolation("sbx-untracked", _spec(), _policy())

    connect.assert_awaited_once()
    assert len(sandbox.commands.calls) == 3


async def test_apply_isolation_connect_failure_raises_runtime_error() -> None:
    """A failed reconnect surfaces as a RuntimeError at the invocation point
    (the same failure class the legacy path raises when a step cannot run)."""
    provider = E2BRuntimeProvider(api_key="down-key")

    with (
        patch("e2b.AsyncSandbox.connect", new=AsyncMock(side_effect=OSError("control plane down"))),
        pytest.raises(RuntimeError, match="reconnect to sandbox"),
    ):
        await provider.apply_isolation("sbx-gone", _spec(), _policy())


async def test_apply_isolation_connect_cancellation_propagates() -> None:
    """CancelledError from the reconnect is re-raised, never swallowed."""
    provider = E2BRuntimeProvider(api_key="cancel-key")

    with (
        patch("e2b.AsyncSandbox.connect", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider.apply_isolation("sbx-cancel", _spec(), _policy())


# ---------------------------------------------------------------------------
# 4. IsolationPolicy carrier shape
# ---------------------------------------------------------------------------


def test_isolation_policy_defaults_match_legacy_defaults() -> None:
    """Defaults mirror ``apply_sandbox_policy``'s keyword defaults so an
    omitted control behaves identically on both paths."""
    policy = IsolationPolicy()
    assert policy.read_only is False
    assert policy.git_credentials is None
    assert policy.egress_policy is None
    assert policy.egress_allowlist is None
    assert policy.allowed_hosts is None
    assert policy.command_timeout == 60.0


def test_isolation_policy_is_frozen() -> None:
    """The policy is an immutable value object (parity inputs cannot be
    mutated between the two invocation paths)."""
    policy = IsolationPolicy()
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        policy.read_only = True  # type: ignore[misc]
    assert policy.read_only is False
