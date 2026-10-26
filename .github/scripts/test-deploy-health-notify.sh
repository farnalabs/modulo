#!/usr/bin/env bash
# Proof for the shared deploy-health email transition (FAR-2139).
#
# Drives `.github/scripts/deploy-health-notify.sh` with synthetic verdicts and
# asserts the EXACT transition an operator experiences:
#
#   healthy  -> healthy   : no email
#   healthy  -> unhealthy : ONE alert email
#   unhealthy-> unhealthy : no duplicate email
#   unhealthy-> healthy   : ONE recovery email
#   unknown  -> unhealthy : alert (UNKNOWN never suppresses)
#   unknown  -> unknown   : no email, loud warning
#   unhealthy-> healthy   : no SMTP configured -> loud warning, no silent skip
#
# No network, no GitHub, no SMTP: the script runs with DRY_RUN=1, which
# resolves the transition and renders the real email body without sending.
#
# Usage: bash .github/scripts/test-deploy-health-notify.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

NOTIFY=".github/scripts/deploy-health-notify.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
NO_TRANSITION_LOG="No transition"
SUBJECT_FIELD='"subject"'
NO_TRANSITION_LABEL="no transition logged"
NO_EMAIL_LABEL="no email rendered"
ALERT_SUBJECT="DEPLOY HEALTH UNHEALTHY"
RECOVERY_SUBJECT="DEPLOY HEALTH RECOVERED"
WORKFLOW_NAME="Deploy: Staleness Watchdog"
DEPLOY_URL="https://github.com/farnalabs/modulo/actions/workflows/deploy.yml"

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    echo "  PASS: $label (expected=$expected)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected=$expected, got=$actual)"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "  PASS: $label (contains '$needle')"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (wanted '$needle' in: $haystack)"
    FAIL=$((FAIL + 1))
  fi
}

assert_not_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "  PASS: $label (does not contain '$needle')"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (unexpected '$needle' in: $haystack)"
    FAIL=$((FAIL + 1))
  fi
}

# ---- synthetic verdicts -----------------------------------------------------
# Healthy: prod current, no failure streak.
python3 - "$WORK" <<'PY'
import json
import sys
from pathlib import Path
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from datetime import UTC, datetime

work = Path(sys.argv[1])
loader = SourceFileLoader("deploy_health", ".github/scripts/deploy_health.py")
module = module_from_spec(spec_from_loader("deploy_health", loader))
sys.modules["deploy_health"] = module
loader.exec_module(module)
now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

healthy = module.evaluate(
    main={"sha": "4db7af2000"},
    prod={"git_sha": "4db7af2000", "build_timestamp": "2026-10-25T11:30:00Z"},
    deploy_runs=[{"status": "completed", "conclusion": "success"}],
    now=now,
)
(work / "healthy.json").write_text(json.dumps(healthy.as_dict()), encoding="utf-8")

# FAR-2159: production was readable and current, but the run history was not.
# The tick is UNKNOWN, never healthy, so it can never close an open alert.
partial = module.evaluate(
    main={"sha": "4db7af2000"},
    prod={"git_sha": "4db7af2000", "build_timestamp": "2026-10-25T11:30:00Z"},
    deploy_runs=None,
    now=now,
)
if partial.healthy is not None:
    raise SystemExit(f"a partially readable tick must be UNKNOWN, got healthy={partial.healthy!r}")
(work / "partial.json").write_text(json.dumps(partial.as_dict()), encoding="utf-8")

unhealthy = module.evaluate(
    main={"sha": "4db7af2000"},
    prod={"git_sha": "7b1c064000", "build_timestamp": "2026-10-23T13:00:00Z"},
    deploy_runs=[{"status": "completed", "conclusion": "failure", "headSha": "deadbeef"}] * 2,
    now=now,
)
(work / "unhealthy.json").write_text(json.dumps(unhealthy.as_dict()), encoding="utf-8")

# The watchdog's own condition: self-heal exhausted, neither shared condition.
watchdog = module.evaluate(main=None, prod=None, deploy_runs=[], now=now)
watchdog_dict = watchdog.as_dict()
watchdog_dict.update(
    {
        "healthy": False,
        "summary": "watchdog could not re-dispatch deploy.yml",
        "notes": [
            "the deploy watchdog could not re-dispatch deploy.yml while production was stale"
        ],
    }
)
(work / "watchdog.json").write_text(json.dumps(watchdog_dict), encoding="utf-8")

# The watchdog's recovery state: not escalating while a deploy is still in
# flight. Recorded as healthy (or the in-flight tick would swallow the recovery
# email) with the measured detail replacing the default "production is current"
# claim, which is not yet true.
recovery = module.evaluate(main=None, prod=None, deploy_runs=[], now=now)
recovery_dict = recovery.as_dict()
recovery_dict.update(
    {
        "healthy": True,
        "summary": "the deploy watchdog is not escalating (deploy in flight)",
        "notes": [
            "a deploy run is in progress/queued, so this tick did not compare production against origin/main"
        ],
        "recovery_detail": (
            "A deploy run is in progress, so the pipeline is expected to converge on its own; "
            "the deploy watchdog is not escalating and no action is needed."
        ),
    }
)
(work / "watchdog-recovery.json").write_text(json.dumps(recovery_dict), encoding="utf-8")
PY

run_notify() {
  # run_notify <healthy> <previous> <verdict-json> [extra env...]
  local healthy="$1" previous="$2" verdict="$3"
  shift 3
  env -i \
    PATH="$PATH" \
    HOME="$HOME" \
    DRY_RUN=1 \
    CURRENT_HEALTHY="$healthy" \
    PREVIOUS_VERDICT="$previous" \
    VERDICT_JSON="$verdict" \
    WORKFLOW_NAME="$WORKFLOW_NAME" \
    VERDICT_STEP_NAME="Fail the run when the deploy watchdog is unhealthy" \
    RUN_URL="https://github.com/farnalabs/modulo/actions/runs/1" \
    DEPLOY_URL="$DEPLOY_URL" \
    "$@" \
    bash "$NOTIFY"
}

echo "=== FAR-2139 proof: shared deploy-health email transition ==="
echo ""

echo "Case 1: healthy -> healthy (no transition, no email)"
OUT="$(run_notify true healthy "$WORK/healthy.json" 2>&1)"
assert_contains "${NO_TRANSITION_LABEL}" "${NO_TRANSITION_LOG}" "$OUT"
assert_not_contains "${NO_EMAIL_LABEL}" "${SUBJECT_FIELD}" "$OUT"
echo ""

echo "Case 2: healthy -> unhealthy (alert exactly once)"
OUT="$(run_notify false healthy "$WORK/unhealthy.json" 2>&1)"
assert_contains "alert subject" "$ALERT_SUBJECT" "$OUT"
assert_contains "reason names the failure streak" "consecutive failed runs" "$OUT"
assert_contains "reason names the stale production" "production is serving" "$OUT"
echo ""

echo "Case 3: unhealthy -> unhealthy (no duplicate email)"
OUT="$(run_notify false unhealthy "$WORK/unhealthy.json" 2>&1)"
assert_contains "no transition logged" "${NO_TRANSITION_LOG}" "$OUT"
assert_not_contains "no duplicate email" "${SUBJECT_FIELD}" "$OUT"
echo ""

echo "Case 4: unhealthy -> healthy (recovery closes the alert)"
OUT="$(run_notify true unhealthy "$WORK/healthy.json" 2>&1)"
assert_contains "recovery subject" "$RECOVERY_SUBJECT" "$OUT"
assert_contains "recovery body" "Deploy health has RECOVERED" "$OUT"
echo ""

echo "Case 5: UNKNOWN previous -> unhealthy (never suppresses the first alert)"
OUT="$(run_notify false unknown "$WORK/unhealthy.json" 2>&1)"
assert_contains "alert still sent" "$ALERT_SUBJECT" "$OUT"
echo ""

echo "Case 6: UNKNOWN current (no transition inferred, loud warning, no email)"
OUT="$(run_notify "" unhealthy "$WORK/unhealthy.json" 2>&1)"
assert_contains "warning emitted" "::warning::" "$OUT"
assert_not_contains "no email rendered" "${SUBJECT_FIELD}" "$OUT"
echo ""

echo "Case 6b: a PARTIAL read takes the UNKNOWN path - no recovery, alert stays open"
OUT="$(run_notify "" unhealthy "$WORK/partial.json" 2>&1)"
assert_contains "warning emitted" "::warning::" "$OUT"
assert_contains "warning says the alert stays open" "live alert therefore stays open" "$OUT"
assert_not_contains "no recovery email" "DEPLOY HEALTH RECOVERED" "$OUT"
assert_not_contains "no email rendered" "${SUBJECT_FIELD}" "$OUT"
echo ""

echo "Case 7: watchdog's own condition (shared renderer, caller-measured detail)"
OUT="$(run_notify false healthy "$WORK/watchdog.json" 2>&1)"
assert_contains "alert subject names the watchdog condition" "watchdog could not re-dispatch deploy.yml" "$OUT"
assert_contains "body carries the measured detail" "could not re-dispatch deploy.yml while production was stale" "$OUT"
assert_not_contains "body never claims health" "all measured deploy-health conditions are healthy" "$OUT"
echo ""

echo "Case 8: a watchdog recovery survives an in-flight deploy tick, with accurate wording"
OUT="$(run_notify true unhealthy "$WORK/watchdog-recovery.json" 2>&1)"
assert_contains "recovery emitted" "$RECOVERY_SUBJECT" "$OUT"
assert_contains "subject names the real evidence" "not escalating (deploy in flight)" "$OUT"
assert_not_contains "never claims production is current" "production is current at" "$OUT"
echo ""

echo "Case 9: a transition with no SMTP config warns loudly, never silently skips"
OUT="$(env -i PATH="$PATH" HOME="$HOME" \
  CURRENT_HEALTHY=false PREVIOUS_VERDICT=healthy \
  VERDICT_JSON="$WORK/unhealthy.json" \
  RUN_URL="https://example.invalid/run" DEPLOY_URL="https://example.invalid/deploy" \
  bash "$NOTIFY" 2>&1)"
RC=$?
assert_eq "exit code (best-effort)" 0 "$RC"
assert_contains "warning names the missing secrets" "NO email was sent" "$OUT"
echo ""

# ---- FAR-2157: the previous-verdict walk skips unmeasured ticks ------------
# A tick that measured nothing carries the UNKNOWN-tick marker step and skips
# the verdict step; reading only the most recent completed run recorded it as
# HEALTHY, which swallowed the recovery email. These cases drive the real
# lookup with a fake `gh` so the walk is exercised (the cases above override
# PREVIOUS_VERDICT and never reach it).
mkdir -p "$WORK/bin"

python3 - "$WORK" <<'PY2'
import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

work = Path(sys.argv[1])
loader = SourceFileLoader("deploy_health", ".github/scripts/deploy_health.py")
module = module_from_spec(spec_from_loader("deploy_health", loader))
sys.modules["deploy_health"] = module
loader.exec_module(module)

# 201 = the NEWEST previous run: measured nothing (UNKNOWN marker, no verdict step).
# 202 = the run before it: measured UNHEALTHY (verdict step failed).
(work / "jobs-201.json").write_text(
    json.dumps({"jobs": [{"steps": [{"name": module.UNKNOWN_VERDICT_STEP_NAME, "conclusion": "success"}]}]}),
    encoding="utf-8",
)
(work / "jobs-202.json").write_text(
    json.dumps(
        {"jobs": [{"steps": [{"name": module.VERDICT_STEP_NAME, "conclusion": "failure"}]}]}
    ),
    encoding="utf-8",
)
(work / "runs.json").write_text(
    json.dumps(
        [
            {"databaseId": 201, "status": "completed", "conclusion": "success"},
            {"databaseId": 202, "status": "completed", "conclusion": "failure"},
        ]
    ),
    encoding="utf-8",
)
PY2

cat > "$WORK/bin/gh" <<'FAKEGH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" = "run" && "${2:-}" = "list" ]]; then
  printf '%s' "${FAKE_RUNS:-[]}"
elif [[ "${1:-}" = "api" ]]; then
  id="$(printf '%s' "${2:-}" | sed -E 's#.*/runs/([0-9]+)/jobs#\1#')"
  case "$id" in
    201) cat "${FAKE_JOBS_DIR}/jobs-201.json" ;;
    202) cat "${FAKE_JOBS_DIR}/jobs-202.json" ;;
    *) echo '{"jobs":[]}' ;;
  esac
else
  false
fi
FAKEGH
chmod +x "$WORK/bin/gh"

run_notify_lookup() {
  # run_notify_lookup <healthy> <verdict-json> -- real run-history lookup path
  local healthy="$1" verdict="$2"
  env -i \
    PATH="$WORK/bin:$PATH" \
    HOME="$HOME" \
    DRY_RUN=1 \
    CURRENT_HEALTHY="$healthy" \
    VERDICT_JSON="$verdict" \
    WORKFLOW_NAME="$WORKFLOW_NAME" \
    VERDICT_STEP_NAME="Fail the run when the deploy watchdog is unhealthy" \
    REPOSITORY="farnalabs/modulo" \
    GITHUB_RUN_ID="999" \
    FAKE_RUNS="$(cat "$WORK/runs.json")" \
    FAKE_JOBS_DIR="$WORK" \
    RUN_URL="https://github.com/farnalabs/modulo/actions/runs/999" \
    DEPLOY_URL="$DEPLOY_URL" \
    bash "$NOTIFY"
}

echo "Case 10: an unmeasured tick between an alert and its recovery no longer swallows the recovery"
OUT="$(run_notify_lookup true "$WORK/healthy.json" 2>&1)"
assert_contains "walk skips the unmeasured run" "Run 201 measured nothing (UNKNOWN)" "$OUT"
assert_contains "walk finds the measured unhealthy verdict" "Previous verdict read from run 202." "$OUT"
assert_contains "recovery email still sent" "DEPLOY HEALTH RECOVERED" "$OUT"
echo ""

echo "Case 11: an unmeasured tick between two unhealthy ticks does not re-send the alert"
OUT="$(run_notify_lookup false "$WORK/unhealthy.json" 2>&1)"
assert_contains "walk finds the measured unhealthy verdict" "Previous verdict read from run 202." "$OUT"
assert_contains "no duplicate alert" "No transition" "$OUT"
assert_not_contains "no email rendered" "${SUBJECT_FIELD}" "$OUT"
echo ""

echo "=== passed: $PASS, failed: $FAIL ==="
if [[ "$FAIL" -ne 0 ]]; then
  exit 1
fi
exit 0
