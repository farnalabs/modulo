#!/bin/bash
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.
#
# Thresholds calibrated against measured branch-coverage baseline (FAR-923):
# each floor is 2-3 points below the measured combined (line+branch) value.
#
# FAR-2112 - why the unit suite is a single run:
# The `backend-test` job runs on ubicloud-standard-4 (4 vCPU / 16 GB). With
# `-n auto` (4 xdist workers) the suite peaks at ~11.7 GB RSS, comfortably
# inside the runner, so the two-shard split FAR-2081 introduced for the old
# 8 GB ubicloud-standard-2 buys nothing - while costing a second pytest
# start-up (`bringing up nodes...` is ~4 min on this suite). The shard split
# becomes REQUIRED again if this runner is ever downsized below ~12 GB.

set -euo pipefail

PARALLELISM="${1:-auto}"

echo "::group::Running unit tests (overall coverage threshold: 89%)"
# Defence in depth for the gated-suite warning gate installed by
# tests/conftest.py::pytest_runtest_setup. A leaked never-awaited
# coroutine only surfaces at GC as PytestUnraisableExceptionWarning; left as a
# warning, a large enough flood of them wedged pytest's terminal writer
# (BlockingIOError: [Errno 11]) and took the 2026-10-21 merge queue down.
# The same escalation is asserted by
# tests/architecture/test_unit_unraisable_warning_gate.py.
uv run --no-sync pytest tests/unit/ -n "$PARALLELISM" --cov=src/modulo --cov-report=xml --cov-report=term-missing --cov-fail-under=89 -q -W "error::pytest.PytestUnraisableExceptionWarning" --disable-warnings
echo "::endgroup::"

echo "::group::Per-module coverage checks"

echo "Checking modulo.auth (threshold: 93%)"
uv run --no-sync --no-build coverage report --include="src/modulo/auth/*" --fail-under=93

echo "Checking modulo.core.pipeline_engine (threshold: 91%)"
uv run --no-sync --no-build coverage report --include="src/modulo/core/pipeline_engine/*" --fail-under=91

echo "Checking modulo.db.rls (threshold: 95%)"
uv run --no-sync --no-build coverage report --include="src/modulo/db/rls.py" --fail-under=95

echo "::endgroup::"
echo "All coverage thresholds met."
