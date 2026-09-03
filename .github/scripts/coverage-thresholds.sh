#!/bin/bash
# Floors are COMBINED line+branch (branch=true since FAR-571); ratchet back toward 90 per the coverage campaign (FAR-575).
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.

set -euo pipefail

PARALLELISM="${1:-auto}"

echo "::group::Running unit tests (overall coverage threshold: 82%)"
uv run --no-sync pytest tests/unit/ -n "$PARALLELISM" --cov=src/modulo --cov-report=xml --cov-report=term-missing --cov-fail-under=82 -q
echo "::endgroup::"

echo "::group::Per-module coverage checks"

echo "Checking modulo.auth (threshold: 95%)"
uv run --no-sync --no-build coverage report --include="src/modulo/auth/*" --fail-under=95

echo "Checking modulo.core.pipeline_engine (threshold: 88%)"
uv run --no-sync --no-build coverage report --include="src/modulo/core/pipeline_engine/*" --fail-under=88

echo "Checking modulo.db.rls (threshold: 98%)"
uv run --no-sync --no-build coverage report --include="src/modulo/db/rls.py" --fail-under=98

echo "::endgroup::"
echo "All coverage thresholds met."
