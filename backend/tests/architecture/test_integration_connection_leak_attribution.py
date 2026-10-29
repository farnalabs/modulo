"""Behavioural pin for the integration suite's unclosed-asyncpg attribution hook.

``backend/tests/integration/conftest.py::pytest_runtest_teardown`` forces a
cyclic collection at each test's teardown so an asyncpg connection that nothing
closes is surfaced against the test that leaked it, instead of an arbitrary
later test. Two properties are load-bearing and were both broken in the first
implementation:

- the hook must be CHAINED, not replace pytest's own ``sys.unraisablehook``.
  A replacement leaves pytest's per-session deque empty when its ``trylast``
  ``collect_unraisable`` drains it inside the same teardown, so the events are
  forwarded afterwards and pytest raises them against the NEXT item at setup -
  the innocent-test blame the hook exists to remove;
- the forced ``gc.collect()`` runs after that drain point, so any non-asyncpg
  unraisable it surfaces must be attributed to THIS item, not left in the deque.

These are end-to-end pins: a real pytest subprocess loads the real hook (and the
real terminal-summary renderer) from the integration conftest into a scratch
rootdir. A leak in test A must leave the downstream tests green and name A; a
collateral unraisable in test C must fail C, not D; and
``MODULO_INTEGRATION_STRICT_CONNECTION_LEAKS=1`` must fail the leaking test.

Prove-it-fails: replacing the chained hook with ``sys.unraisablehook =
captured.append`` makes ``test_collateral_unraisable_fails_the_leaking_test``
report an error on ``test_d_innocent_after_collateral`` (or a warnings summary)
instead of ``test_c_collateral_unraisable``; dropping the forced ``gc.collect()``
makes ``test_asyncpg_leak_is_attributed_and_quarantined`` lose the terminal
summary entry for ``test_a_leaks_asyncpg_connection``; removing the strict-mode
branch makes ``test_strict_mode_fails_the_leaking_test`` see ``test_a`` pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
GATE_WARNING = "error::pytest.PytestUnraisableExceptionWarning"

_CONFTEST = """
from tests.integration.conftest import (
    pytest_runtest_teardown,
    pytest_terminal_summary,
)
"""

_TEST_MODULE = """
class _LeakedConnection:
    '''Stand-in for asyncpg's Connection.__del__ ResourceWarning.'''

    def __del__(self):
        raise ResourceWarning(
            "unclosed connection <asyncpg.connection.Connection object at 0xdeadbeef>"
        )


class _CollateralUnraisable:
    '''A non-asyncpg unraisable, which the gate must keep failing.'''

    def __del__(self):
        raise ValueError("collateral boom")


def test_a_leaks_asyncpg_connection():
    leaked = _LeakedConnection()
    leaked.cycle = leaked
    del leaked


def test_b_innocent_after_asyncpg_leak():
    assert True


def test_c_collateral_unraisable():
    collateral = _CollateralUnraisable()
    collateral.cycle = collateral
    del collateral


def test_d_innocent_after_collateral():
    assert True
"""


def _run_scratch_suite(tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    rootdir = tmp_path / "scratch"
    rootdir.mkdir()
    (rootdir / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (rootdir / "test_leaks.py").write_text(_TEST_MODULE, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(BACKEND), env.get("PYTHONPATH")]))
    env.pop("MODULO_INTEGRATION_STRICT_CONNECTION_LEAKS", None)
    env.update(extra_env)

    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no untrusted input
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
        timeout=120,
        check=False,
    )


def test_asyncpg_leak_is_attributed_and_quarantined(tmp_path: Path) -> None:
    result = _run_scratch_suite(tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    # The asyncpg-leaking test itself stays green; the only error is the
    # deliberately collateral unraisable raised by test C (see the test below).
    assert "4 passed, 1 error" in output, output
    assert "test_d_innocent_after_collateral" not in output.split("short test summary info")[-1], output
    # ... and the leak is reported against the test that actually leaked it.
    assert "test_a_leaks_asyncpg_connection" in output
    assert "unclosed asyncpg connection" in output


def test_collateral_unraisable_fails_the_leaking_test(tmp_path: Path) -> None:
    result = _run_scratch_suite(tmp_path)

    output = result.stdout + result.stderr
    # The non-asyncpg unraisable is still gated, but at test C - the test that
    # caused it - never at the innocent test D that follows.
    assert "test_c_collateral_unraisable" in output
    assert "4 passed, 1 error" in output, output
    assert "collateral boom" in output
    assert "test_d_innocent_after_collateral" not in output.split("short test summary info")[-1], output


def test_strict_mode_fails_the_leaking_test(tmp_path: Path) -> None:
    result = _run_scratch_suite(tmp_path, MODULO_INTEGRATION_STRICT_CONNECTION_LEAKS="1")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_a_leaks_asyncpg_connection" in output
    assert "unclosed asyncpg connection leaked by" in output
