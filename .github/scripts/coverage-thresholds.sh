#!/bin/bash
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.
#
# Thresholds calibrated against measured branch-coverage baseline (FAR-923):
# each floor is 2-3 points below the measured combined (line+branch) value.
#
# FAR-2081 - why the unit suite is run in two shards:
# A single `pytest tests/unit ... -n auto` invocation peaked at ~9.8 GB RSS on
# the ubicloud-standard-2 CI runner (2 vCPU / 8 GB): the pytest controller plus
# each of the two xdist workers holds its own copy of the collected-test state
# and coverage data. The runner was shut down near the end of the suite (8/8
# reproductions on a PR). Running `tests/unit` as two sequential shards roughly
# halves what any one pytest tree holds. The shard datasets are COMBINED before
# any threshold is evaluated, so every threshold (the overall 89% floor plus the
# per-module floors) still applies to the whole suite - never to one shard.

set -euo pipefail

PARALLELISM="${1:-auto}"

# Distinct per-shard data files, so the second shard cannot overwrite the first.
# `coverage combine` then merges `.coverage.shard*` into the canonical
# `.coverage` that the report/XML steps read.
COVERAGE_BASE=".coverage"
SHARD_DATA_PREFIX="${COVERAGE_BASE}.shard"

# Start from a clean slate: a stale dataset or report from an earlier run must
# never be combined with this one.
rm -f "${COVERAGE_BASE}" "${COVERAGE_BASE}".* coverage.xml

# Enumerate the unit test files the way pytest does. pyproject.toml sets no
# `python_files`, so pytest's defaults apply and BOTH `test_*.py` and
# `*_test.py` are collected. Matching only `test_*.py` would silently drop any
# `*_test.py` file - and therefore its coverage - from both shards, so the
# split would not be lossless.
mapfile -t TEST_FILES < <(
  find tests/unit -type f \( -name 'test_*.py' -o -name '*_test.py' \) | sort
)

if [ "${#TEST_FILES[@]}" -eq 0 ]; then
  echo "ERROR: no unit test files found under tests/unit" >&2
  exit 1
fi

# Split into two contiguous halves; the first half takes the extra file on an
# odd count.
SHARD1_COUNT=$(( (${#TEST_FILES[@]} + 1) / 2 ))
SHARD1_FILES=("${TEST_FILES[@]:0:${SHARD1_COUNT}}")
SHARD2_FILES=("${TEST_FILES[@]:${SHARD1_COUNT}}")

# An empty second shard would make pytest fall back to its configured
# testpaths and run the WHOLE suite twice - doubling memory instead of halving
# it. Fail loudly rather than degrade silently.
if [ "${#SHARD2_FILES[@]}" -eq 0 ]; then
  echo "ERROR: shard split produced an empty second shard" >&2
  exit 1
fi

run_shard() {
  local label="$1"
  local data_file="$2"
  shift 2
  local files=("$@")

  echo "::group::Running unit tests - shard ${label} (${#files[@]} files)"
  # Defence in depth for the unit-suite gate installed by
  # tests/unit/conftest.py::pytest_runtest_setup. A leaked never-awaited
  # coroutine only surfaces at GC as PytestUnraisableExceptionWarning; left as a
  # warning, a large enough flood of them wedged pytest's terminal writer
  # (BlockingIOError: [Errno 11]) and took the 2026-10-21 merge queue down.
  # The same escalation is asserted by
  # tests/architecture/test_unit_unraisable_warning_gate.py.
  #
  # --cov-fail-under=0 is required: pyproject.toml's [tool.coverage.report]
  # fail_under = 89 is honoured by pytest-cov on EVERY run, so without this a
  # single shard would fail on its own partial total (e.g. 2.37%) even though
  # the combined coverage is above the floor. The 89% floor is enforced exactly
  # once, below, against the combined dataset.
  COVERAGE_FILE="${data_file}" uv run --no-sync pytest "${files[@]}" \
    -n "$PARALLELISM" --cov=src/modulo --cov-report= --cov-fail-under=0 -q \
    -W "error::pytest.PytestUnraisableExceptionWarning"
  echo "::endgroup::"
}

run_shard "1/2" "${SHARD_DATA_PREFIX}1" "${SHARD1_FILES[@]}"
run_shard "2/2" "${SHARD_DATA_PREFIX}2" "${SHARD2_FILES[@]}"

echo "::group::Combining shard coverage (overall coverage threshold: 89%)"
uv run --no-sync --no-build coverage combine
# `coverage report --show-missing` on the combined dataset prints the same
# missing-lines table that `--cov-report=term-missing` used to provide, and
# enforces the overall floor against the WHOLE suite.
uv run --no-sync --no-build coverage report --show-missing --fail-under=89
# coverage.xml is consumed downstream (the diff-cover coverage gate and
# SonarCloud), so it must describe the whole suite, never a single shard.
uv run --no-sync --no-build coverage xml -o coverage.xml
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
