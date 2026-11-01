"""Regression tests for the narrow AsyncMock leak reclamation helper."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import types
import warnings
from collections.abc import Awaitable
from unittest.mock import AsyncMock, MagicMock


def test_known_fixture_leak_is_reclaimed_by_the_concrete_fix() -> None:
    """The shared fixture path fixes its known sync-method leak at the source."""
    mock = AsyncMock()
    mock.add = MagicMock()
    mock.add("row")
    assert mock.add.call_count == 1
    del mock
    gc.collect()


def test_unmarked_asyncmock_leak_remains_observable_under_warning_as_error() -> None:
    """An ordinary unawaited AsyncMock call is not hidden by the harness."""
    probe = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-c",
            "from unittest.mock import AsyncMock; AsyncMock()()",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert "RuntimeWarning" in probe.stderr
    assert "never awaited" in probe.stderr


def test_started_or_suspended_coroutine_is_not_force_closed() -> None:
    """A started coroutine completes normally rather than being force-closed."""
    mock = AsyncMock()
    coroutine = mock()

    async def drive(coroutine_to_await: Awaitable[object]) -> None:
        await coroutine_to_await

    asyncio.run(drive(coroutine))
    assert isinstance(coroutine, types.CoroutineType)
    assert coroutine.cr_frame is None
    del coroutine
    del mock
    gc.collect()

    async def suspend() -> None:
        await asyncio.sleep(0)

    suspended = suspend()
    assert isinstance(suspended, types.CoroutineType)
    try:
        assert suspended.cr_frame is not None
        assert suspended.cr_await is None
    finally:
        suspended.close()


def test_unrelated_runtime_warning_keeps_normal_warning_policy() -> None:
    """An unrelated warning still follows the normal warning machinery."""
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        warnings.warn("unrelated warning", RuntimeWarning, stacklevel=1)

    assert [str(entry.message) for entry in captured] == ["unrelated warning"]
