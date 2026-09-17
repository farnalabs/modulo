#!/bin/bash
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.
#
# Thresholds calibrated against measured branch-coverage baseline (FAR-923):
# each floor is 2-3 points below the measured combined (line+branch) value.

set -euo pipefail

PARALLELISM="${1:-auto}"

echo "::group::Running unit tests (overall coverage threshold: 89%)"
uv run --no-sync pytest tests/unit/ -n "$PARALLELISM" --cov=src/modulo --cov-report=xml --cov-report=term-missing --cov-fail-under=89 -q
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
