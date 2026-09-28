"""Regression: direct module imports must not hit circular-import errors.

The hitl_manager -> hitl_email_alerts -> pipeline_engine -> executor ->
hitl_manager cycle (broken by lazy-importing hitl_email_alerts inside
HITLManager.create_gate) caused ImportError when any of these modules
was the FIRST import in a fresh interpreter.  This test spawns a fresh
subprocess for each import path to prove the cycle is broken.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# The import paths that previously triggered the circular-import failure.
# Each must succeed in a FRESH interpreter (no shared module cache).
_IMPORT_PATHS: list[str] = [
    "import modulo.api.routes.admin",
    "import modulo.core.pipeline_engine",
    "import modulo.core.hitl_manager",
]


@pytest.mark.parametrize("import_stmt", _IMPORT_PATHS, ids=lambda s: s.split(".")[-1])
def test_direct_import_succeeds_in_fresh_interpreter(import_stmt: str) -> None:
    """A fresh Python process importing *import_stmt* must exit 0.

    Uses subprocess to avoid any contamination from the test-runner's
    already-loaded module cache.
    """
    # The bound guards against an import that HANGS (a genuine cycle), not
    # against import latency. A cold fresh interpreter spends 35-55s simply
    # importing modulo.core.pipeline_engine on this machine when it is
    # otherwise idle, and well past 120s when other test workers are running,
    # so the old 30s bound failed for slowness rather than for a circular
    # import. Measured 2026-09-27 on unmodified main (35.7s) and on the
    # FAR-1050 R6 worktree (45.2s / 52.6s / 55.8s idle; >120s under load) -
    # every run exited 0.
    result = subprocess.run(  # noqa: S603 — testing our own import paths, not user input
        [sys.executable, "-c", import_stmt],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    # Include stdout/stderr in the assertion message for easy debugging.
    combined = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, (
        f"'{import_stmt}' failed in a fresh interpreter (exit {result.returncode}):\n{combined}"
    )
