"""FAR-487/FAR-489: the sandbox LIFETIME reaches ``AsyncSandbox.create``.

The legacy dispatch passed ``timeout=int(sandbox_timeout +
_SANDBOX_LIFETIME_GRACE_S)`` into ``AsyncSandbox.create`` so the sandbox
always outlives the agent command. ``E2BRuntimeProvider.create_workspace``
read ``spec.timeout_seconds`` only as its *provisioning wait bound* and never
forwarded it, so the SDK fell back to ``SandboxBase.default_sandbox_timeout``
(300s) — a node configured for 1200s got a sandbox that died at 300s. These
tests pin the forwarding, the int coercion, and the still-present wait bound.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import WorkspaceSpec
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

# The FAR-487 grace the dispatch adds on top of the node's command timeout
# (node_runner._SANDBOX_LIFETIME_GRACE_S) — 1200 + 120 = 1320.
_GRACE_S = 120
_LONG_COMMAND_TIMEOUT_S = 1200
_LONG_LIFETIME_S = _LONG_COMMAND_TIMEOUT_S + _GRACE_S
# E2B's documented maximum keep-alive for Pro accounts (the SDK create
# docstring: 86_400 seconds / 24 hours).
_E2B_MAX_LIFETIME_S = 86_400


def _spec(**kwargs: Any) -> WorkspaceSpec:
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="ubuntu-22.04",
        **kwargs,
    )


@pytest.fixture
def mock_sandbox() -> MagicMock:
    sbx = MagicMock()
    sbx.sandbox_id = "sbx-lifetime-001"
    sbx.commands = MagicMock()
    sbx.commands.run = AsyncMock()
    sbx.kill = AsyncMock()
    return sbx


@pytest.fixture
def mock_sandbox_cls(mock_sandbox: MagicMock) -> Generator[MagicMock, None, None]:
    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.create = AsyncMock(return_value=mock_sandbox)
        yield mock_cls


def _create_kwargs(mock_cls: MagicMock) -> dict[str, Any]:
    assert mock_cls.create.call_count == 1
    _args, kwargs = mock_cls.create.call_args
    return dict(kwargs)


# ---------------------------------------------------------------------------
# 1. The lifetime reaches the SDK as ``timeout=``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_forwards_spec_lifetime_to_sdk(
    mock_sandbox_cls: MagicMock,
) -> None:
    """The dispatch's lifetime is the exact ``timeout=`` kwarg the SDK gets.

    Fails without the fix: ``create`` is called with no ``timeout=`` at all,
    so the SDK's own 300s default applies.
    """
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(timeout_seconds=_LONG_LIFETIME_S))

    kwargs = _create_kwargs(mock_sandbox_cls)
    assert kwargs["timeout"] == _LONG_LIFETIME_S


@pytest.mark.asyncio
async def test_long_lifetime_does_not_get_the_sdk_300s_default(
    mock_sandbox_cls: MagicMock,
) -> None:
    """A 1200s node (+120s grace) must NOT collapse to the SDK default.

    Compared against the SDK's real ``SandboxBase.default_sandbox_timeout``
    so the assertion cannot drift from the library it is guarding.
    """
    from e2b.sandbox.main import SandboxBase

    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(timeout_seconds=_LONG_LIFETIME_S))

    kwargs = _create_kwargs(mock_sandbox_cls)
    assert kwargs["timeout"] == _LONG_LIFETIME_S
    assert kwargs["timeout"] != SandboxBase.default_sandbox_timeout
    assert SandboxBase.default_sandbox_timeout == 300
    # Strictly greater than the command timeout — the FAR-487 guarantee.
    assert kwargs["timeout"] > _LONG_COMMAND_TIMEOUT_S


@pytest.mark.asyncio
async def test_provider_adds_no_grace_of_its_own(
    mock_sandbox_cls: MagicMock,
) -> None:
    """The provider forwards the lifetime verbatim; the dispatch owns the grace."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(timeout_seconds=_LONG_COMMAND_TIMEOUT_S))

    kwargs = _create_kwargs(mock_sandbox_cls)
    assert kwargs["timeout"] == _LONG_COMMAND_TIMEOUT_S
    assert kwargs["timeout"] != _LONG_LIFETIME_S


# ---------------------------------------------------------------------------
# 2. Type and range of the forwarded value (FAR-489)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forwarded_lifetime_is_an_int_never_a_float(
    mock_sandbox_cls: MagicMock,
) -> None:
    """A float lifetime is coerced to ``int`` before it reaches the SDK.

    The E2B attrs model does not coerce, and the Go server rejects
    ``"1320.0"`` with a 400 (int32 unmarshal) — every create would fail.
    """
    provider = E2BRuntimeProvider(api_key="sk-test")
    spec = _spec(timeout_seconds=_LONG_LIFETIME_S)
    # Simulate a float leaking in from a caller/config (the FAR-489 class).
    spec.timeout_seconds = cast(int, float(_LONG_LIFETIME_S))
    assert isinstance(spec.timeout_seconds, float)  # precondition: raw float

    await provider.create_workspace(spec)

    kwargs = _create_kwargs(mock_sandbox_cls)
    forwarded = kwargs["timeout"]
    assert type(forwarded) is int  # type() is int: excludes bool as well
    assert forwarded == _LONG_LIFETIME_S


@pytest.mark.asyncio
async def test_forwarded_lifetime_is_within_the_sdk_accepted_range(
    mock_sandbox_cls: MagicMock,
) -> None:
    """The largest lifetime a sandbox node can carry stays in SDK range.

    GraphValidator caps ``timeout_seconds`` at 3300 (FAR-511), so the
    dispatched lifetime is at most 3300 + 120 = 3420 — well under E2B's
    documented 86_400s ceiling, and never negative or zero.
    """
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(timeout_seconds=3300 + _GRACE_S))

    kwargs = _create_kwargs(mock_sandbox_cls)
    forwarded = kwargs["timeout"]
    assert 0 < forwarded <= _E2B_MAX_LIFETIME_S


@pytest.mark.asyncio
async def test_non_positive_lifetime_is_not_forwarded_and_is_logged(
    mock_sandbox_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No lifetime supplied -> the kwarg is omitted LOUDLY, never ``0``.

    Forwarding ``0`` (or a negative) would be rejected by the SDK/Go server;
    silently defaulting to 300s is the very defect this fix removes, so the
    fallback is logged as a warning instead of happening unnoticed.
    """
    provider = E2BRuntimeProvider(api_key="sk-test")
    with caplog.at_level("WARNING"):
        await provider.create_workspace(_spec(timeout_seconds=0))

    kwargs = _create_kwargs(mock_sandbox_cls)
    assert "timeout" not in kwargs
    assert "not a positive lifetime" in caplog.text


# ---------------------------------------------------------------------------
# 3. The provisioning wait bound is still applied (separate concern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisioning_wait_bound_still_fires(
    mock_sandbox_cls: MagicMock,
) -> None:
    """A wedged create call is still cut off by the ``asyncio.wait_for`` bound.

    Fails if the bound were dropped while forwarding the lifetime: the
    hanging ``AsyncSandbox.create`` would never return.
    """
    provider = E2BRuntimeProvider(api_key="sk-test")

    async def _hang(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(10)
        return object()

    mock_sandbox_cls.create = AsyncMock(side_effect=_hang)
    with pytest.raises(RuntimeError, match=r"Timed out after 1s provisioning E2B sandbox"):
        await provider.create_workspace(_spec(timeout_seconds=1))


@pytest.mark.asyncio
async def test_provisioning_wait_bound_is_passed_to_wait_for(
    mock_sandbox_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create call sits under a finite ``asyncio.wait_for`` bound.

    Spied rather than waited out: a long lifetime must not turn the wait
    bound into ``None`` (unbounded) now that the same value is also the
    forwarded ``timeout=``.
    """
    captured: dict[str, Any] = {}
    real_wait_for = asyncio.wait_for

    async def _spy(aw: Any, **kwargs: Any) -> Any:
        captured["timeout"] = kwargs.get("timeout")
        return await real_wait_for(aw, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", _spy)

    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(_spec(timeout_seconds=_LONG_LIFETIME_S))

    assert captured["timeout"] is not None
    assert captured["timeout"] == _LONG_LIFETIME_S
    # ...and the same call forwarded the lifetime to the SDK.
    assert _create_kwargs(mock_sandbox_cls)["timeout"] == _LONG_LIFETIME_S


@pytest.mark.asyncio
async def test_missing_lifetime_falls_back_to_the_provision_bound(
    mock_sandbox_cls: MagicMock,
) -> None:
    """With no lifetime the wait bound still resolves to ``_MAX_PROVISION_TIMEOUT``.

    The bound must stay finite even when there is nothing to forward — the
    pre-fix fallback is preserved for the create CALL (not for the lifetime).
    """
    from modulo.core.runtime_provider.e2b import _MAX_PROVISION_TIMEOUT

    captured: dict[str, Any] = {}
    real_wait_for = asyncio.wait_for

    async def _spy(aw: Any, **kwargs: Any) -> Any:
        captured["timeout"] = kwargs.get("timeout")
        return await real_wait_for(aw, **kwargs)

    with patch.object(asyncio, "wait_for", _spy):
        provider = E2BRuntimeProvider(api_key="sk-test")
        await provider.create_workspace(_spec(timeout_seconds=0))

    assert captured["timeout"] == _MAX_PROVISION_TIMEOUT
