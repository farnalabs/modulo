#!/usr/bin/env bash
# Proof test for restart-wedged-app-machines.sh (2026-10-23 staging outage).
#
# Usage: bash .github/scripts/test-restart-wedged-app-machines.sh
#
# Runs the real script against a fake `fly` on PATH. No Fly API, no network.
# Pins the invariants that matter:
#   1. a started app-group machine with a CRITICAL app_liveness check is restarted;
#   2. healthy / non-started / worker-group / servicecheck-only machines are NOT;
#   3. an unreadable or unparseable machine list takes no action and exits 0;
#   4. a FAILED restart destroys the same machine (a machine that cannot be
#      restarted cannot be deployed onto - the next attempt must create a
#      replacement), while a SUCCESSFUL restart never destroys anything;
#   5. every failure is best-effort: warning, exit 0 (the deploy attempt is
#      the authoritative gate and must not be masked);
#   6. the wedged machine's boot log is captured (credentials redacted) BEFORE
#      it is restarted/destroyed, so a destroy cannot discard the only evidence
#      of why the app process group never became healthy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SCRIPT_DIR}/restart-wedged-app-machines.sh"
APP="staging-modulo"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
BIN_DIR="${WORK_DIR}/bin"
mkdir -p "${BIN_DIR}"
RESTART_LOG="${WORK_DIR}/restarts.log"
DESTROY_LOG="${WORK_DIR}/destroys.log"
LOGS_LOG="${WORK_DIR}/logs.log"
APP_LOG_DIR="${WORK_DIR}/app-machine-logs"
: > "${RESTART_LOG}"
: > "${DESTROY_LOG}"
: > "${LOGS_LOG}"

cat > "${BIN_DIR}/fly" <<'FAKE_FLY'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  logs)
    echo "$*" >> "${FLY_LOGS_LOG:?}"
    echo "boot log fetched"
    echo "DATABASE_URL fixed: postgresql://modulo_app:hunter2@db.internal:5432/modulo..."
    echo "DATABASE_URL fixed: postgresql+asyncpg://staging_modulo:asyncpw@[fdaa:8b:fa9e:0:1::4]:5432/st..."
    echo "MODULO_SYSTEM_DATABASE_URL fixed: postgresql+psycopg://modulo_system:psycopgsecret@db.internal:5432/st..."
    exit "${FLY_LOGS_EXIT:-0}"
    ;;
  machines)
    if [ "${2:-}" = "list" ]; then
      if [ "${FLY_MACHINES_JSON:-}" = "__FAIL__" ]; then
        exit 1
      fi
      printf '%s' "${FLY_MACHINES_JSON:-[]}"
      exit 0
    fi
    ;;
  machine)
    if [ "${2:-}" = "restart" ]; then
      echo "${3:-UNKNOWN}" >> "${FLY_RESTART_LOG:?}"
      exit "${FLY_RESTART_EXIT:-0}"
    fi
    if [ "${2:-}" = "destroy" ]; then
      echo "${3:-UNKNOWN}" >> "${FLY_DESTROY_LOG:?}"
      exit "${FLY_DESTROY_EXIT:-0}"
    fi
    ;;
esac
echo "fake fly: unexpected args: $*" >&2
exit 64
FAKE_FLY
chmod +x "${BIN_DIR}/fly"

PASS=0
FAIL=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    echo "  PASS: ${label}"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: ${label} (expected='${expected}', got='${actual}')"
    FAIL=$((FAIL + 1))
  fi
}

# run_case <name> <machines-json> [restart-exit]
run_case() {
  local name="$1" json="$2" restart_exit="${3:-0}" destroy_exit="${4:-0}"
  echo "=== ${name} ==="
  : > "${RESTART_LOG}"
  : > "${DESTROY_LOG}"
  : > "${LOGS_LOG}"
  rm -rf "${APP_LOG_DIR}"
  set +e
  OUTPUT="$(PATH="${BIN_DIR}:${PATH}" \
    FLY_RESTART_LOG="${RESTART_LOG}" \
    FLY_DESTROY_LOG="${DESTROY_LOG}" \
    FLY_LOGS_LOG="${LOGS_LOG}" \
    FLY_MACHINES_JSON="${json}" \
    FLY_RESTART_EXIT="${restart_exit}" \
    FLY_DESTROY_EXIT="${destroy_exit}" \
    WEDGED_LOG_DIR="${APP_LOG_DIR}" \
    bash "${TARGET}" "${APP}" 2>&1)"
  RC=$?
  set -e
  RESTARTED="$(tr '\n' ' ' < "${RESTART_LOG}" | sed 's/ *$//')"
  DESTROYED="$(tr '\n' ' ' < "${DESTROY_LOG}" | sed 's/ *$//')"
  LOGGED="$(tr '\n' ' ' < "${LOGS_LOG}" | sed 's/ *$//')"
  echo "  stdout: $(printf '%s' "${OUTPUT}" | tr '\n' ' ')"
}

machine_json() {
  # machine_json <id> <state> <group> <check-name> <check-status>
  python3 - "$@" <<'PY'
import json, sys
mid, state, group, check_name, check_status = sys.argv[1:6]
print(json.dumps([{
    "id": mid,
    "state": state,
    "config": {"metadata": {"fly_process_group": group}},
    "checks": [{"name": check_name, "status": check_status}],
}]))
PY
}

# --- 1. Wedged app machine is restarted -------------------------------------
run_case "started app machine with critical app_liveness is restarted" \
  "$(machine_json 84e90ea2239e68 started app app_liveness critical)"
assert_eq "exit code" "0" "${RC}"
assert_eq "restarted machine" "84e90ea2239e68" "${RESTARTED}"
assert_eq "no destroy after a successful restart" "" "${DESTROYED}"
assert_eq "wedged machine log captured before restart" \
  "logs --app staging-modulo --machine 84e90ea2239e68 --no-tail" "${LOGGED}"
assert_eq "captured log is written for the deploy artifact upload" "yes" \
  "$([ -f "${APP_LOG_DIR}/84e90ea2239e68.log" ] && echo yes || echo no)"
assert_eq "named password redacted from the captured log" "0" \
  "$(grep -c 'hunter2' "${APP_LOG_DIR}/84e90ea2239e68.log" || true)"
assert_eq "driver-qualified asyncpg password redacted from the captured log" "0" \
  "$(grep -c 'asyncpw' "${APP_LOG_DIR}/84e90ea2239e68.log" || true)"
assert_eq "driver-qualified psycopg password redacted from the captured log" "0" \
  "$(grep -c 'psycopgsecret' "${APP_LOG_DIR}/84e90ea2239e68.log" || true)"
assert_eq "redaction keeps the IPv6 host and database path" "1" \
  "$(grep -c 'postgresql+asyncpg://staging_modulo:\*\*\*@\[fdaa:8b:fa9e:0:1::4\]:5432/st' "${APP_LOG_DIR}/84e90ea2239e68.log" || true)"
assert_eq "redacted log keeps its diagnostic content" "1" \
  "$(grep -c 'boot log fetched' "${APP_LOG_DIR}/84e90ea2239e68.log" || true)"

# --- 2. Healthy app machine is left alone -----------------------------------
run_case "healthy app machine is not restarted" \
  "$(machine_json 84e90ea2239e68 started app app_liveness passing)"
assert_eq "exit code" "0" "${RC}"
assert_eq "no restart" "" "${RESTARTED}"
assert_eq "no log fetch for a healthy machine" "" "${LOGGED}"

# --- 3. Worker-group machine is never restarted -----------------------------
run_case "critical worker machine is not restarted" \
  "$(machine_json 8d9e11be931148 started worker app_liveness critical)"
assert_eq "no restart" "" "${RESTARTED}"

# --- 4. Deployment-scoped servicecheck is never the trigger -----------------
run_case "critical servicecheck alone does not restart" \
  "$(machine_json 84e90ea2239e68 started app servicecheck-00-http-8080 critical)"
assert_eq "no restart" "" "${RESTARTED}"

# --- 5. Non-started machine is not restarted --------------------------------
run_case "stopped machine is not restarted" \
  "$(machine_json 84e90ea2239e68 stopped app app_liveness critical)"
assert_eq "no restart" "" "${RESTARTED}"

# --- 6. Unreadable machine list: warn, no action, exit 0 --------------------
run_case "failed machine list is best-effort no-op" "__FAIL__"
assert_eq "exit code" "0" "${RC}"
assert_eq "no restart" "" "${RESTARTED}"
assert_eq "warns" "1" "$(printf '%s' "${OUTPUT}" | grep -c '::warning::' || true)"

# --- 7. Unparseable machine list: warn, no action, exit 0 -------------------
run_case "unparseable machine list is best-effort no-op" "not-json"
assert_eq "exit code" "0" "${RC}"
assert_eq "no restart" "" "${RESTARTED}"

# --- 8. Failed restart destroys the machine so the retry can replace it -----
run_case "failed restart destroys the machine for replacement" \
  "$(machine_json 84e90ea2239e68 started app app_liveness critical)" 1
assert_eq "exit code" "0" "${RC}"
assert_eq "restart attempted" "84e90ea2239e68" "${RESTARTED}"
assert_eq "destroyed machine" "84e90ea2239e68" "${DESTROYED}"
assert_eq "warns" "1" "$(printf '%s' "${OUTPUT}" | grep -c '::warning::' || true)"

# --- 9. Failed destroy is best-effort: warning + exit 0 ---------------------
run_case "failed destroy does not mask the deploy attempt" \
  "$(machine_json 84e90ea2239e68 started app app_liveness critical)" 1 1
assert_eq "exit code" "0" "${RC}"
assert_eq "destroy attempted" "84e90ea2239e68" "${DESTROYED}"
assert_eq "warns" "2" "$(printf '%s' "${OUTPUT}" | grep -c '::warning::' || true)"

# --- 10. No wedge means no restart and no destroy ---------------------------
run_case "healthy app machine is never destroyed" \
  "$(machine_json 84e90ea2239e68 started app app_liveness passing)"
assert_eq "no destroy" "" "${DESTROYED}"

echo ""
echo "=== summary: ${PASS} passed, ${FAIL} failed ==="
[ "${FAIL}" -eq 0 ]
