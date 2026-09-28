"""FAR-1050 R6: ``get_metrics`` — the ABC resource-metrics primitive + E2B.

Exercises, without a live sandbox or network:

1. The ABC's ``get_metrics`` default raises the typed
   ``ProviderCapabilityUnsupportedError`` (ADR 040 error honesty — never a
   raw ``NotImplementedError``) for a provider that does not override it.
   That refusal is what the resource-cap watchdog fails OPEN on, so a tier
   without a metrics substrate degrades to a warning, never a crash.
2. ``E2BRuntimeProvider.get_metrics`` polls the SDK's
   ``sandbox.get_metrics()`` and maps every sample onto the
   provider-neutral ``WorkspaceMetrics`` carrier (values preserved,
   unobservable fields -> ``None``), keeping the SDK's sample order.
3. Handle resolution mirrors the other primitives: tracked handle first,
   otherwise reconnect by ref.
4. Poll failures PROPAGATE (the watchdog owns the fail-open; the
   primitive never swallows a measurement failure into an empty reading).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import (
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    RuntimeProviderError,
    WorkspaceMetrics,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider


class _NoMetricsProvider(RuntimeProvider):
    """Concrete provider that does NOT override ``get_metrics``."""

    provider_id = "no-metrics"

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ref"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> object:
        raise AssertionError("exec_command must not be called in this test")

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


def _sdk_sample(**overrides: object) -> SimpleNamespace:
    """An SDK ``SandboxMetrics``-shaped sample (the attribute names it ships)."""
    sample = {
        "cpu_count": 2,
        "cpu_used_pct": 87.5,
        "disk_total": 10 * 1024**3,
        "disk_used": 3 * 1024**3,
        "mem_total": 4 * 1024**3,
        "mem_used": 1024**3,
    }
    sample.update(overrides)
    return SimpleNamespace(**sample)


def _provider_with_tracked(sandbox: object, ref: str) -> E2BRuntimeProvider:
    provider = E2BRuntimeProvider(api_key="sk-test")
    provider._sandboxes[ref] = sandbox
    return provider


# ---------------------------------------------------------------------------
# 1. ABC default — typed capability refusal (error honesty)
# ---------------------------------------------------------------------------


async def test_abc_default_get_metrics_raises_typed_capability_unsupported() -> None:
    """ADR 040 error honesty: the default raises the TYPED refusal, not NotImplementedError."""
    with pytest.raises(ProviderCapabilityUnsupportedError, match="get_metrics") as exc_info:
        await _NoMetricsProvider().get_metrics("sbx-ref")

    assert isinstance(exc_info.value, RuntimeProviderError)
    assert not isinstance(exc_info.value, NotImplementedError)
    assert "_NoMetricsProvider" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. WorkspaceMetrics carrier
# ---------------------------------------------------------------------------


def test_workspace_metrics_defaults_are_none() -> None:
    """Unobservable metrics read as ``None`` — never a fabricated 0."""
    sample = WorkspaceMetrics()
    assert sample.cpu_used_pct is None
    assert sample.cpu_count is None
    assert sample.mem_used is None
    assert sample.disk_used is None


def test_workspace_metrics_is_frozen() -> None:
    """The carrier is an immutable value object (parity across providers)."""
    sample = WorkspaceMetrics(cpu_used_pct=10.0)
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        sample.cpu_used_pct = 90.0  # type: ignore[misc]
    assert sample.cpu_used_pct == 10.0


def test_workspace_metrics_exported_in_all() -> None:
    import modulo.core.runtime_provider as pkg

    assert "WorkspaceMetrics" in pkg.__all__
    assert pkg.WorkspaceMetrics is WorkspaceMetrics


# ---------------------------------------------------------------------------
# 3. E2B implementation — maps the SDK's metrics
# ---------------------------------------------------------------------------


async def test_e2b_get_metrics_returns_the_sdk_samples_mapped() -> None:
    """The primitive returns the SDK's metrics, carrier-mapped value-for-value."""
    fake = MagicMock()
    fake.get_metrics = AsyncMock(
        return_value=[
            _sdk_sample(cpu_used_pct=12.5, mem_used=2 * 1024**3),
            _sdk_sample(cpu_used_pct=87.5),
        ]
    )
    provider = _provider_with_tracked(fake, "sbx-metrics-1")

    samples = await provider.get_metrics("sbx-metrics-1")

    fake.get_metrics.assert_awaited_once_with()
    # SDK sample order preserved (oldest-first), newest last for the killer.
    assert [s.cpu_used_pct for s in samples] == [12.5, 87.5]
    assert samples[0].mem_used == float(2 * 1024**3)
    assert samples[1].mem_used == float(1024**3)
    assert samples[1].cpu_count == 2
    assert samples[1].disk_used == float(3 * 1024**3)
    assert samples[1].disk_total == float(10 * 1024**3)
    assert samples[1].mem_total == float(4 * 1024**3)
    assert all(isinstance(s, WorkspaceMetrics) for s in samples)


async def test_e2b_get_metrics_maps_unobservable_fields_to_none() -> None:
    """A non-numeric / absent SDK field becomes ``None``, not a number to compare."""
    fake = MagicMock()
    fake.get_metrics = AsyncMock(return_value=[_sdk_sample(cpu_used_pct="n/a", mem_used=None, disk_used=True)])
    provider = _provider_with_tracked(fake, "sbx-metrics-2")

    (sample,) = await provider.get_metrics("sbx-metrics-2")

    assert sample.cpu_used_pct is None
    assert sample.mem_used is None
    assert sample.disk_used is None


async def test_e2b_get_metrics_empty_sample_list_returns_empty() -> None:
    """No samples yet is a measurement gap, not an error."""
    fake = MagicMock()
    fake.get_metrics = AsyncMock(return_value=[])
    provider = _provider_with_tracked(fake, "sbx-metrics-3")

    assert not await provider.get_metrics("sbx-metrics-3")


async def test_e2b_get_metrics_reconnects_by_ref_when_untracked() -> None:
    """Untracked ref -> AsyncSandbox.connect(ref), same by-ref shape as file I/O."""
    fake = MagicMock()
    fake.get_metrics = AsyncMock(return_value=[_sdk_sample()])
    provider = E2BRuntimeProvider(api_key="sk-test")

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(return_value=fake)
        samples = await provider.get_metrics("sbx-orphan-7")

    mock_cls.connect.assert_awaited_once_with("sbx-orphan-7", api_key="sk-test")
    assert len(samples) == 1
    assert samples[0].cpu_used_pct == 87.5


async def test_e2b_get_metrics_propagates_reconnect_failure() -> None:
    """A poll failure propagates — the WATCHDOG owns the fail-open, not this primitive."""
    provider = E2BRuntimeProvider(api_key="sk-test")

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(side_effect=RuntimeError("control plane down"))
        with pytest.raises(RuntimeError, match="get_metrics"):
            await provider.get_metrics("sbx-dead-1")


async def test_e2b_get_metrics_poll_timeout_propagates() -> None:
    """A wedged metrics endpoint surfaces as TimeoutError (bounded by _METRICS_TIMEOUT)."""
    fake = MagicMock()
    fake.get_metrics = AsyncMock(side_effect=TimeoutError())
    provider = _provider_with_tracked(fake, "sbx-metrics-4")

    with pytest.raises(TimeoutError):
        await provider.get_metrics("sbx-metrics-4")
