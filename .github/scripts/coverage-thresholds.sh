#!/bin/bash
# Runs unit tests with per-module coverage threshold enforcement.
# Exit code is non-zero if any threshold is breached.
#
# Thresholds calibrated against measured branch-coverage baseline (FAR-923):
# each floor is 2-3 points below the measured combined (line+branch) value.
#
# Three modes (second argument, default "run"):
#
#   run      - one job runs the whole tests/unit suite and enforces every
#              threshold in-process. The single-job fallback, and still the
#              shape a manual/local invocation uses.
#   shard    - one of N CONCURRENT jobs: runs only its slice of tests/unit into
#              its own parallel data file and applies NO thresholds of ANY kind -
#              neither the whole-suite floor nor the per-module floors. A shard
#              covers a fraction of the tree, so either kind of floor is
#              meaningless there (it would fail at ~30% no matter how correct
#              the code is). The shard exits 0 whenever its tests pass.
#   combine  - merges every shard's data file (`coverage combine`), writes the
#              coverage.xml the changed-lines gate and SonarCloud consume, and
#              only THEN enforces the identical thresholds against the merged
#              report.
#
# Why the shards exist: the unit suite is MEMORY-bound, not CPU-bound. Each
# xdist worker holds a large in-process fixture graph, so four workers on a
# 16 GB runner already peak at ~11.7 GB RSS - adding workers to one runner does
# not cut wall-clock, it only swaps CPU idling for swap. Three independent
# 16 GB runners in parallel each run the same 4 workers on a third of the tree,
# which is the only lever that shortens the critical path without raising the
# per-runner memory ceiling. (FAR-2081's earlier two-shard split was reverted
# because those halves ran SEQUENTIALLY in one runner: two pytest start-ups for
# no wall-clock win. These three run concurrently, which is the point.)
#
# Sharding is deterministic and exact: the sorted tests/unit test-file list is
# dealt round-robin (file k -> shard ((k-1) % N) + 1), so every file runs in
# exactly one shard and the split is reproducible across runners. Each shard
# passes `--ignore` for the files belonging to the other shards. That keeps the
# pytest root at `tests/unit` - the whole-suite-root invariant
# tests/architecture/test_unit_unraisable_warning_gate.py pins - instead of
# replacing the root with a computed file list, which would silently narrow what
# the command appears to cover.

set -euo pipefail

PARALLELISM="${1:-auto}"
MODE="${2:-run}"

check_module_thresholds() {
  echo "::group::Per-module coverage checks"

  echo "Checking modulo.auth (threshold: 93%)"
  uv run --no-sync --frozen --no-build coverage report --include="src/modulo/auth/*" --fail-under=93

  echo "Checking modulo.core.pipeline_engine (threshold: 91%)"
  uv run --no-sync --frozen --no-build coverage report --include="src/modulo/core/pipeline_engine/*" --fail-under=91

  echo "Checking modulo.db.rls (threshold: 95%)"
  uv run --no-sync --frozen --no-build coverage report --include="src/modulo/db/rls.py" --fail-under=95

  echo "::endgroup::"
  echo "All coverage thresholds met."
}

case "$MODE" in
  run | shard | combine) ;;
  *)
    echo "coverage-thresholds.sh: unknown mode '$MODE' (expected run|shard|combine)" >&2
    exit 2
    ;;
esac

if [ "$MODE" = "combine" ]; then
  echo "::group::Combining unit coverage shards"
  # Each shard names its own data file (COVERAGE_FILE=.coverage.unit-<n>), and
  # [tool.coverage.run] parallel = true additionally suffix-tags any data file a
  # coverage invocation writes, so concurrent writers can never clobber one
  # another. Both shapes match the `.coverage.*` glob `coverage combine` reads,
  # which folds them into the single .coverage the reports below consume.
  uv run --no-sync --frozen --no-build coverage combine
  # --fail-under=0 on the XML: the whole-suite floor is enforced once, below, by
  # the explicit `coverage report` (the [tool.coverage.report] fail_under config
  # would otherwise also fire here, failing the job on the report step that only
  # exists to publish the artifact).
  uv run --no-sync --frozen --no-build coverage xml -o coverage.xml --fail-under=0
  # The whole-suite floor lives here (not in the shard runs), against the
  # merged report - the same 89% `run` mode enforces in-process.
  uv run --no-sync --frozen --no-build coverage report --fail-under=89
  echo "::endgroup::"
  check_module_thresholds
  exit 0
fi

COV_ARGS=(--cov=src/modulo)
SHARD_ARGS=()

if [ "$MODE" = "shard" ]; then
  SHARD="${COVERAGE_SHARD:?COVERAGE_SHARD must be set in shard mode (e.g. 1/3)}"
  GROUP="${SHARD%%/*}"
  TOTAL="${SHARD##*/}"
  if ! [[ "$GROUP" =~ ^[0-9]+$ && "$TOTAL" =~ ^[0-9]+$ ]] || [ "$GROUP" -lt 1 ] || [ "$GROUP" -gt "$TOTAL" ]; then
    echo "coverage-thresholds.sh: COVERAGE_SHARD must be '<group>/<total>' with 1 <= group <= total (got '$SHARD')" >&2
    exit 2
  fi
  # Named per shard so the uploaded artifact is attributable, and so a re-run of
  # one shard can never be merged into another shard's data.
  export COVERAGE_FILE=".coverage.unit-${GROUP}"
  # --cov-fail-under=0: defer the whole-suite floor to `combine`. Without this
  # the [tool.coverage.report] fail_under=89 config would fail every shard.
  COV_ARGS+=(--cov-report=term-missing --cov-fail-under=0)
  INDEX=0
  while IFS= read -r test_file; do
    INDEX=$((INDEX + 1))
    if [ $(((INDEX - 1) % TOTAL + 1)) -ne "$GROUP" ]; then
      SHARD_ARGS+=("--ignore=${test_file}")
    fi
  done < <(find tests/unit -type f -name 'test_*.py' | LC_ALL=C sort)
  echo "::notice::unit shard ${GROUP}/${TOTAL}: ${#SHARD_ARGS[@]} file(s) ignored, COVERAGE_FILE=${COVERAGE_FILE}"
else
  COV_ARGS+=(--cov-report=xml --cov-report=term-missing --cov-fail-under=89)
fi

echo "::group::Running unit tests (overall coverage threshold: 89%)"
# Defence in depth for the gated-suite warning gate installed by
# tests/conftest.py::pytest_runtest_setup. A leaked never-awaited
# coroutine only surfaces at GC as PytestUnraisableExceptionWarning; left as a
# warning, a large enough flood of them wedged pytest's terminal writer
# (BlockingIOError: [Errno 11]) and took the 2026-10-21 merge queue down.
# The same escalation is asserted by
# tests/architecture/test_unit_unraisable_warning_gate.py.
uv run --no-sync --frozen pytest tests/unit/ -n "$PARALLELISM" "${COV_ARGS[@]}" -q -W "error::pytest.PytestUnraisableExceptionWarning" --disable-warnings ${SHARD_ARGS[@]+"${SHARD_ARGS[@]}"}
echo "::endgroup::"

if [ "$MODE" = "run" ]; then
  # pytest-cov normally leaves ONE data file (it merges xdist workers itself),
  # so there is usually nothing to combine here. If the [tool.coverage.run]
  # parallel setting is ever honoured instead, the run leaves suffixed files and
  # the CLI reports below need them folded into .coverage first. Combine only
  # when there is something to combine, so the no-suffixed-file case does not
  # trip `set -e` on coverage's "No data to combine" exit.
  if compgen -G '.coverage.*' > /dev/null; then
    uv run --no-sync --frozen --no-build coverage combine
  fi
  # The per-module floors belong to the modes that see the WHOLE tree: `run`
  # (single-job fallback, everything in one data file) and `combine` (enforced
  # above against the merged report). They must never run in `shard` mode: a
  # shard only executes its third of tests/unit, so it measures a fraction of
  # every module's branches and a whole-module floor fails there by
  # construction, no matter how correct the code is. (Observed in the first
  # sharded run, 41598150598: shard 1 measured 69% over the files it happened
  # to touch and `--fail-under=93` for modulo.auth failed the job while every
  # test passed.) Both thresholds - the 89 whole-suite floor and these
  # 93/91/95 per-module floors - stay exactly as they are in `run`/`combine`.
  check_module_thresholds
elif [ "$MODE" = "shard" ]; then
  # No threshold is enforced in shard mode, at any level: pytest ran with
  # --cov-fail-under=0 (so [tool.coverage.report] fail_under can never fire)
  # and the per-module floors are deliberately skipped. A shard's only job is
  # to produce its data file and exit 0 when its tests pass; the single
  # authoritative gate is `coverage combine`.
  echo "::notice::unit shard ${COVERAGE_SHARD}: coverage thresholds deferred to the combine job (no threshold is enforced per shard)"
fi
