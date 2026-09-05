"""Unit tests for executor._teardown_hub runtime-provider disposal branch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from modulo.core.pipeline_engine.executor import _teardown_hub


async def test_teardown_hub_aclose_runtime_provider() -> None:
    """When the hub carries a RuntimeProviderHub it is aclosed at teardown."""
    hub = AsyncMock()
    hub._runtime_provider = AsyncMock()

    await _teardown_hub(hub)

    hub.__aexit__.assert_awaited_once()
    hub._runtime_provider.aclose.assert_awaited_once()


async def test_teardown_hub_continues_when_aclose_raises() -> None:
    """A failure disposing the runtime provider is logged and never masks cleanup."""
    hub = AsyncMock()
    runtime = AsyncMock()
    runtime.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    hub._runtime_provider = runtime

    # Must not raise; the hub __aexit__ still completed.
    await _teardown_hub(hub)

    hub.__aexit__.assert_awaited_once()
    runtime.aclose.assert_awaited_once()


async def test_teardown_hub_skips_aclose_for_bare_provider() -> None:
    """A bare provider (no aclose) is left to GC; only __aexit__ runs."""
    hub = AsyncMock()
    hub._runtime_provider = SimpleNamespace()

    await _teardown_hub(hub)

    hub.__aexit__.assert_awaited_once()
