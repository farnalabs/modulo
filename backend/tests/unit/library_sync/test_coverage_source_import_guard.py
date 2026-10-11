"""Regression guard: single-module coverage source specs must not crash the suite.

Historical failure (reproduced on 2026-10-11): running pytest with a
single-module coverage source, e.g.

    pytest tests/unit/library_sync/test_sync.py \\
        --cov=modulo.core.library_service.community --cov-report=

errored at fixture setup on every test with
``KeyError: 'pydantic.root_model'`` raised inside
``pydantic._internal._generics.create_generic_submodel``.

Mechanism: pytest-cov starts coverage during ``pytest_load_initial_conftests``
— before the backend conftests are imported. When coverage is given a
module-name source spec, coverage.py's
``InOrOut.set_matchers_depending_on_syspath()`` resolves it with ``find_spec``
(which executes the target's parent packages) inside ``sys_modules_saved()``,
and ``SysModuleSaver.restore()`` then deletes every module imported during
that block from ``sys.modules``. The executed chain (``library_service`` ->
``community`` -> ``library_sync`` -> ``settings`` ->
``pydantic_settings.sources.utils``) triggers pydantic's lazy
``pydantic.root_model`` load; the rollback removes the submodule from
``sys.modules`` but leaves the ``RootModel`` attribute bound on the
already-imported ``pydantic`` package. Every later ``from pydantic import
RootModel`` is then satisfied by that dangling attribute, so the submodule is
never re-imported, and ``mcp/types.py``'s import-time
``class JSONRPCMessage(RootModel[...])`` crashes on the missing sys.modules
entry.

The residency guard in ``tests/conftest.py`` (``import pydantic.root_model``)
re-establishes the sys.modules entry after the rollback. This test replays the
historical failing invocation in a subprocess: real pytest-cov, module-name
source spec, the real root conftest, then a probe plugin that imports the
crash-site module ``mcp.types`` at ``pytest_sessionstart`` (after conftests
load, matching the real failure's ordering). --collect-only keeps it fast —
the crash never depended on test execution, only on import ordering. The test
fails if the KeyError (or any session-start crash) ever returns.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[3]

# Probe plugin loaded into the subprocess via -p: imports the historical
# crash-site module after conftests have loaded. If the residency guard in
# tests/conftest.py stops working, this import dies with the original KeyError
# and pytest reports an INTERNALERROR without MCP_TYPES_IMPORT_OK.
_COV_GUARD_PROBE_PLUGIN = '''\
"""Import the crash-site module after conftests load; KeyError here = regression."""


def pytest_sessionstart(session):
    import mcp.types  # noqa: F401

    print("MCP_TYPES_IMPORT_OK", flush=True)
'''

_COV_SOURCE = "modulo.core.library_service.community"


def test_single_module_cov_source_import_does_not_strand_pydantic_root_model(
    tmp_path: Path,
) -> None:
    """The historical failing ``--cov=<module>`` sequence must not crash at session start."""
    probe_dir = tmp_path / "plugins"
    probe_dir.mkdir()
    (probe_dir / "cov_guard_probe.py").write_text(_COV_GUARD_PROBE_PLUGIN)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(probe_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'coverage-source-guard.db'}"
    env.setdefault("SECRET_KEY", "a" * 32)
    env.setdefault("FERNET_KEY", "b" * 32)
    # Isolate the subprocess's coverage data file so it never races the parent
    # session's data file when the parent suite itself runs under coverage.
    env["COVERAGE_FILE"] = str(tmp_path / ".coverage")

    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/library_sync/test_sync.py",
            "-p",
            "cov_guard_probe",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:warnings",
            "--no-header",
            "-q",
            "--collect-only",
            f"--cov={_COV_SOURCE}",
            "--cov-report=",
            # The KeyError is the regression under guard; the repo-wide
            # fail_under=89 gate is meaningless here (collection executes no
            # test code, so coverage stays at 0%), so pin it to zero instead
            # of failing on unrelated coverage math.
            "--cov-fail-under=0",
            "-o",
            "addopts=",
        ],
        cwd=_BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert "KeyError: 'pydantic.root_model'" not in output, (
        "Single-module --cov source import stranded pydantic.root_model out of "
        "sys.modules (coverage SysModuleSaver rollback vs pydantic's lazy "
        "root_model load), so mcp.types' import-time RootModel[...] crashes. "
        "The tests/conftest.py pydantic.root_model residency guard must keep "
        "working. Subprocess output:\n" + output
    )
    assert "MCP_TYPES_IMPORT_OK" in output, (
        f"Subprocess did not reach the mcp.types import (rc={proc.returncode}).\n{output}"
    )
    assert proc.returncode == 0, f"Subprocess exited {proc.returncode} (expected 0).\n{output}"
