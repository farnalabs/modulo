#!/bin/bash
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.
#
# Thresholds are calibrated against the measured branch-coverage baseline
# (FAR-923): each floor is set 2-3 points below the measured value to
# provide headroom without flaking.

set -euo pipefail

PARALLELISM="${1:-auto}"

echo "::group::Running unit tests (overall coverage threshold: 58%)"
uv run --no-sync pytest tests/unit/ -n "$PARALLELISM" --cov=src/modulo --cov-report=xml --cov-report=term-missing --cov-fail-under=58 -q
echo "::endgroup::"

echo "::group::Per-module coverage checks"

echo "Checking modulo.auth (threshold: 93%)"
uv run --no-sync --no-build coverage report --include="src/modulo/auth/*" --fail-under=93

echo "Checking modulo.core.pipeline_engine (threshold: 51%)"
uv run --no-sync --no-build coverage report --include="src/modulo/core/pipeline_engine/*" --fail-under=51

echo "Checking modulo.db.rls (threshold: 94%)"
uv run --no-sync --no-build coverage report --include="src/modulo/db/rls.py" --fail-under=94

echo "::endgroup::"
echo "All coverage thresholds met."
