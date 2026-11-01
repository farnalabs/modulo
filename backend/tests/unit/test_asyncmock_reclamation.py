"""Regression tests for the narrow AsyncMock leak reclamation helper."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import types
import warnings
from collections.abc import Awaitable
from unittest.mock import AsyncMock


def test_known_fixture_leak_is_reclaimed_by_the_concrete_fix() -> None:
    """The shared BDD session harness prevents its known sync-method leak."""
    old_shape = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-c",
            (
                "import gc; from unittest.mock import AsyncMock; "
                "session = AsyncMock(); session.add('row'); del session; gc.collect()"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert "RuntimeWarning" in old_shape.stderr
    assert "never awaited" in old_shape.stderr

    concrete_fix = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-c",
            (
                "import gc; from tests.bdd.conftest import make_mock_session; "
                "session = make_mock_session(); session.add('row'); del session; gc.collect()"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert concrete_fix.returncode == 0
    assert "RuntimeWarning" not in concrete_fix.stderr
    assert "never awaited" not in concrete_fix.stderr


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
