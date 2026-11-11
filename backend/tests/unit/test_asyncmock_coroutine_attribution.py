"""Behavioural pins for the AsyncMock coroutine-leak attribution fixture.

``backend/tests/unit/conftest.py`` wraps ``AsyncMockMixin._execute_mock_call``
so every coroutine an ``AsyncMock`` call produces is recorded, and inspects the
coroutines recorded during a test at that test's teardown. A coroutine that
never started is a never-awaited call, and the fixture fails the test that
CREATED it with the creation stack.

That matters because CPython only reports an un-awaited coroutine when the
object is finalised. In the 2026-11-01 flake the orphan was held in the
request frame's locals (kept alive by the exception traceback), so it was
finalised during an arbitrary LATER test on the same xdist worker - failing
``test_run_admission``, ``trigger_engine``, ``scim``, ``deploy_workflow_rehearsal``
and ``test_run_validate_nav_config`` while the real leak was in
``tests/unit/api/test_self_lockout.py``.

The subprocess pins load the REAL unit conftest as a plugin into a scratch
rootdir, so they exercise the shipped hook - not a copy of it. The in-process
pins cover the predicate and the rendering directly.

Prove-it-fails: dropping the ``pytest.fail`` from
``_attribute_leaked_asyncmock_coroutines`` makes the leak probe exit 0 (or fail
the innocent follower through the standard warning gate) instead of naming
``test_b``; dropping ``sys.set_coroutine_origin_tracking_depth`` loses
``Coroutine created at`` and the guilty source line.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit import conftest as unit_conftest

BACKEND = Path(__file__).resolve().parents[2]
GATE_WARNING = "error::pytest.PytestUnraisableExceptionWarning"

_SCRATCH_CONFTEST = """pytest_plugins = ["tests.unit.conftest"]\n"""

_LEAK_MODULE = """
from unittest.mock import AsyncMock


def test_a_innocent():
    assert True


def test_b_leaks_asyncmock_coroutine():
    session = AsyncMock()
    session.add("row")


def test_c_innocent_after_leak():
    assert True
"""

_AWAITED_MODULE = """
import asyncio
from unittest.mock import AsyncMock


def test_awaited_asyncmock_call_is_clean():
    mock = AsyncMock()
    result = asyncio.run(mock())
    assert isinstance(result, AsyncMock)
"""


def _run_scratch(
    tmp_path: Path,
    module_source: str,
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    """Run *module_source* under the real unit conftest in a scratch rootdir."""
    rootdir = tmp_path / "scratch"
    rootdir.mkdir(parents=True, exist_ok=True)
    (rootdir / "conftest.py").write_text(_SCRATCH_CONFTEST, encoding="utf-8")
    (rootdir / "test_leaks.py").write_text(module_source, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(BACKEND), env.get("PYTHONPATH")]))
    env.pop("FARNA_CONTROL_MODULO_TEST_COROUTINE_ATTRIBUTION", None)
    env.update(extra_env)

    return subprocess.run(  # noqa: S603 - fixed argv, sys.executable, no untrusted input
        [
            sys.executable,
            "-m",
            "pytest",
            "test_leaks.py",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-W",
            GATE_WARNING,
        ],
        cwd=rootdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


async def _never_awaited_probe() -> None:
    """A coroutine factory used by the in-process predicate pins."""
    return


def test_leak_fails_the_creating_test_with_its_creation_stack(tmp_path: Path) -> None:
    """The fixture names the guilty test and renders where the coroutine began."""
    result = _run_scratch(tmp_path, _LEAK_MODULE)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert "ERROR at teardown of test_b_leaks_asyncmock_coroutine" in output
    assert "1 never-awaited AsyncMock coroutine(s) created during" in output
    assert "test_leaks.py::test_b_leaks_asyncmock_coroutine" in output
    assert "Coroutine created at (most recent call last):" in output
    assert 'session.add("row")' in output


def test_innocent_tests_after_a_leak_stay_green(tmp_path: Path) -> None:
    """The leak is charged to its creator; the followers are not collateral."""
    result = _run_scratch(tmp_path, _LEAK_MODULE)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert "3 passed, 1 error" in output
    summary = output.split("short test summary info")[-1]
    assert "test_b_leaks_asyncmock_coroutine" in summary
    assert "test_c_innocent_after_leak" not in summary
    assert "test_a_innocent" not in summary


def test_awaited_asyncmock_coroutines_are_not_reported(tmp_path: Path) -> None:
    """An awaited AsyncMock call is the supported path and must stay green."""
    result = _run_scratch(tmp_path, _AWAITED_MODULE)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "never-awaited AsyncMock coroutine" not in output


def test_opt_out_disables_the_attribution_message(tmp_path: Path) -> None:
    """``MODULO_TEST_COROUTINE_ATTRIBUTION=0`` removes the guard for debugging."""
    result = _run_scratch(tmp_path, _LEAK_MODULE, MODULO_TEST_COROUTINE_ATTRIBUTION="0")
    output = result.stdout + result.stderr

    assert "never-awaited AsyncMock coroutine(s) created during" not in output


def test_origin_tracking_is_enabled_for_the_unit_suite() -> None:
    """The creation stack is recorded by CPython origin tracking, not tracemalloc."""
    if not unit_conftest._ATTRIBUTION_ENABLED:
        pytest.skip("attribution disabled via MODULO_TEST_COROUTINE_ATTRIBUTION")
    assert sys.get_coroutine_origin_tracking_depth() >= unit_conftest._COROUTINE_ORIGIN_DEPTH


def test_predicate_flags_a_never_started_coroutine() -> None:
    """A created-but-unstarted coroutine is exactly the leak shape."""
    coroutine = _never_awaited_probe()
    try:
        assert unit_conftest._is_never_awaited(coroutine)
    finally:
        coroutine.close()


def test_predicate_accepts_a_completed_coroutine() -> None:
    """A coroutine that ran to completion is not a leak."""
    coroutine = _never_awaited_probe()
    asyncio.run(coroutine)

    assert coroutine.cr_frame is None
    assert not unit_conftest._is_never_awaited(coroutine)


def test_creation_stack_names_the_creating_module() -> None:
    """The rendered stack points at this file, which is where it was created."""
    coroutine = _never_awaited_probe()
    try:
        rendered = unit_conftest._format_creation_stack(coroutine)
    finally:
        coroutine.close()

    assert "Coroutine created at (most recent call last):" in rendered
    assert "test_asyncmock_coroutine_attribution.py" in rendered
