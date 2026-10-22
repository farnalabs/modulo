#!/bin/bash
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.
#
# Thresholds calibrated against measured branch-coverage baseline (FAR-923):
# each floor is 2-3 points below the measured combined (line+branch) value.

set -euo pipefail

PARALLELISM="${1:-auto}"

echo "::group::Running unit tests (overall coverage threshold: 89%)"
# Defence in depth for the unit-suite gate installed by
# tests/unit/conftest.py::pytest_runtest_setup. A leaked never-awaited
# coroutine only surfaces at GC as PytestUnraisableExceptionWarning; left as a
# warning, a large enough flood of them wedged pytest's terminal writer
# (BlockingIOError: [Errno 11]) and took the 2026-10-21 merge queue down.
# The same escalation is asserted by
# tests/architecture/test_unit_unraisable_warning_gate.py.
uv run --no-sync pytest tests/unit/ -n "$PARALLELISM" --cov=src/modulo --cov-report=xml --cov-report=term-missing --cov-fail-under=89 -q \
  -W "error::pytest.PytestUnraisableExceptionWarning"
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
