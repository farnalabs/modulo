#!/bin/bash
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.

set -euo pipefail

PARALLELISM="${1:-auto}"

echo "::group::Running unit tests (overall coverage threshold: 61%)"
uv run --no-sync pytest tests/unit/ -n "$PARALLELISM" --cov=src/modulo --cov-report=xml --cov-report=term-missing --cov-fail-under=61 -q
echo "::endgroup::"

echo "::group::Per-module coverage checks"

echo "Checking modulo.auth (threshold: 73%)"
uv run --no-sync coverage report --include="src/modulo/auth/*" --fail-under=73

echo "Checking modulo.core.pipeline_engine (threshold: 43%)"
uv run --no-sync coverage report --include="src/modulo/core/pipeline_engine/*" --fail-under=43

echo "Checking modulo.db.rls (threshold: 83%)"
uv run --no-sync coverage report --include="src/modulo/db/rls.py" --fail-under=83

echo "::endgroup::"
echo "All coverage thresholds met."
