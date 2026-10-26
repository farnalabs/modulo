#!/usr/bin/env bash
# Audit-chain monitor escalation driver (FAR-2160).
#
# ONE implementation for the audit-chain monitor's blind-cycle escalation. The
# state machine, the thresholds and every human-facing message live in
# `.github/scripts/audit_chain_monitor.py` (unit-tested); this script is the I/O
# around it: the run-history walk and the Zoho SMTP send `issue-notify.yml` and
# `deploy-health-notify.sh` already use.
#
# Two modes:
#
#   evaluate  Walk THIS workflow's own run history, decide the blind-cycle
#             state, and write the step outputs. Reads state only; no side
#             effects. State vector: the workflow's own run history through the
#             Actions jobs API (step CONCLUSIONS, not job outputs - FAR-2157).
#             A skipped step is returned by the API and is never a signal.
#
#   email     Render and send the blind/recovery email. DRY_RUN=1 prints the
#             exact rendered payload and sends nothing (the local proof).
#
# The Linear ticket itself is created by the workflow step whose name is the
# durable "already escalated" marker (`audit_chain_monitor.ESCALATION_STEP_NAME`);
# this script renders its title/body via the evaluator's `ticket` subcommand.
#
# Env (evaluate):
#   REPOSITORY, GITHUB_RUN_ID, GH_TOKEN       supplied by the runner
#   CURRENT_AUTHENTICATED                     steps.auth.outputs.authenticated
#   BLIND_THRESHOLD                           consecutive blind cycles (default 1)
#   LOOKUP_BUDGET                             max previous runs to inspect (12)
#   RUNS_JSON, JOBS_DIR                       local-proof overrides (skip gh)
#   GITHUB_OUTPUT                             step outputs
#   RUNNER_TEMP                               where the state JSON is kept
#
# Env (email):
#   STATE_JSON, EMAIL_ACTION, RUN_URL, DRY_RUN
#   ZOHO_SMTP_USER, ZOHO_SMTP_PASS, ISSUE_NOTIFY_EMAIL, ISSUE_NOTIFY_FROM
#
# Exit code: 0 for every best-effort outcome (no transition, SMTP unconfigured,
# unreadable history). 1 only when a payload cannot be rendered - that is a
# defect in the workflow, not a transient condition. The BLIND condition itself
# never fails the run: a red monitor every 6h is noise, and it is designed to be
# read, not to cry wolf.

set -uo pipefail

MODE="${1:-evaluate}"
EVALUATOR="${EVALUATOR:-.github/scripts/audit_chain_monitor.py}"
WORKFLOW_FILE="${WORKFLOW_FILE:-audit-chain-monitor.yml}"
BLIND_THRESHOLD="${BLIND_THRESHOLD:-1}"
LOOKUP_BUDGET="${LOOKUP_BUDGET:-12}"

if [[ ! -f "$EVALUATOR" ]]; then
  echo "::error::The shared audit-chain escalation evaluator is missing ($EVALUATOR) - the blind-cycle escalation cannot run." >&2
  exit 1
fi

emit_output() {
  # GITHUB_OUTPUT is absent when the script runs locally.
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s\n' "$1=$2" >> "$GITHUB_OUTPUT"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------
run_evaluate() {
  local current_authenticated="${CURRENT_AUTHENTICATED:-}"
  local current_blind="unknown"
  case "$current_authenticated" in
    true) current_blind="false" ;;
    false) current_blind="true" ;;
    *) current_blind="unknown" ;;
  esac

  local state_dir="${STATE_DIR:-${RUNNER_TEMP:-/tmp}/audit-chain-escalation}"
  rm -rf "$state_dir"
  mkdir -p "$state_dir/jobs"
  local history_ndjson="$state_dir/history.ndjson"
  : > "$history_ndjson"

  local history_read="true"
  local history_exhausted="false"
  local runs_file=""
  local looked_up=0

  if [[ -n "${RUNS_JSON:-}" && -f "${RUNS_JSON}" ]]; then
    runs_file="$RUNS_JSON"
  elif [[ -n "${REPOSITORY:-}" ]] && command -v gh >/dev/null 2>&1; then
    if gh run list --repo "$REPOSITORY" --workflow "$WORKFLOW_FILE" --limit 30 \
        --json databaseId,status,conclusion,createdAt,url > "$state_dir/runs.json" 2>/dev/null; then
      runs_file="$state_dir/runs.json"
    fi
  fi

  if [[ -z "$runs_file" ]]; then
    # Fail LOUD, never healthy: the evaluator escalates the current blind cycle.
    echo "::error::Could not read the audit-chain monitor's run history - the escalation state is UNKNOWN. A blind cycle still escalates rather than being assumed healthy (FAR-2160)." >&2
    history_read="false"
  else
    local run_rows
    run_rows="$(python3 - "$runs_file" "${GITHUB_RUN_ID:-}" "$LOOKUP_BUDGET" <<'PY'
import json
import sys

path, current, budget = sys.argv[1], str(sys.argv[2]), int(sys.argv[3])
try:
    with open(path, encoding="utf-8") as handle:
        runs = json.load(handle)
except Exception:
    sys.exit(0)

if not isinstance(runs, list):
    sys.exit(0)

seen = 0
for run in runs:
    if str(run.get("status") or "").lower() != "completed":
        continue
    if str(run.get("databaseId")) == current:
        continue
    if seen >= budget:
        break
    seen += 1
    print(
        "\t".join(
            [
                str(run.get("databaseId") or ""),
                str(run.get("createdAt") or ""),
                str(run.get("url") or ""),
            ]
        )
    )
PY
)" || run_rows=""

    local incomplete="false"
    local hit_boundary="false"
    while IFS=$'\t' read -r run_id run_created run_url; do
      [[ -z "$run_id" ]] && continue
      local jobs_file="$state_dir/jobs/$run_id.json"
      if [[ -n "${JOBS_DIR:-}" && -f "${JOBS_DIR}/$run_id.json" ]]; then
        cp "${JOBS_DIR}/$run_id.json" "$jobs_file"
      elif [[ -n "${REPOSITORY:-}" ]] && command -v gh >/dev/null 2>&1; then
        gh api "repos/$REPOSITORY/actions/runs/$run_id/jobs" > "$jobs_file" 2>/dev/null || rm -f "$jobs_file"
      fi
      if [[ ! -f "$jobs_file" ]]; then
        echo "::warning::Could not read run ${run_id}'s jobs - treating the previous state as UNKNOWN, which escalates rather than suppresses."
        incomplete="true"
        break
      fi
      local record
      record="$(python3 "$EVALUATOR" describe-run \
        --jobs-json "$jobs_file" \
        --run-id "$run_id" \
        --created-at "$run_created" \
        --run-url "$run_url")" || record=""
      rm -f "$jobs_file"
      if [[ -z "$record" ]]; then
        echo "::warning::Could not classify run ${run_id} - treating the previous state as UNKNOWN."
        incomplete="true"
        break
      fi
      printf '%s\n' "$record" >> "$history_ndjson"
      local state
      state="$(printf '%s' "$record" | jq -r '.state')"
      looked_up=$((looked_up + 1))
      if [[ "$state" != "blind" ]]; then
        hit_boundary="true"
        break
      fi
    done <<< "$run_rows"

    if [[ "$incomplete" = "true" ]]; then
      # An unreadable run is UNKNOWN, not a gap to skip. Drop what we read so
      # the evaluator cannot infer a streak from a partial window; it escalates
      # the current blind cycle instead.
      : > "$history_ndjson"
      history_read="false"
    elif [[ "$hit_boundary" != "true" && "$looked_up" -ge "$LOOKUP_BUDGET" ]]; then
      # The window filled while every run we saw was blind: the streak may be
      # longer than we can see. The evaluator marks the count incomplete.
      history_exhausted="true"
    fi
  fi

  if ! jq -s '.' "$history_ndjson" > "$state_dir/history.json" 2>/dev/null; then
    echo "::error::Could not assemble the audit-chain run-history state - escalating rather than assuming healthy." >&2
    printf '[]\n' > "$state_dir/history.json"
    history_read="false"
  fi
  rm -rf "$state_dir/jobs"

  python3 "$EVALUATOR" evaluate \
    --current-blind "$current_blind" \
    --history "$state_dir/history.json" \
    --threshold "$BLIND_THRESHOLD" \
    --history-read "$history_read" \
    --history-exhausted "$history_exhausted" \
    --github-output "${GITHUB_OUTPUT:-}" \
    --state-out "$state_dir/state.json"
}

# ---------------------------------------------------------------------------
# email
# ---------------------------------------------------------------------------
run_email() {
  local state_json="${STATE_JSON:-}"
  local action="${EMAIL_ACTION:-}"
  local run_url="${RUN_URL:-}"

  if [[ ! -f "$state_json" ]]; then
    echo "::error::Audit-monitor email transition ($action) but the state JSON is missing ($state_json) - cannot render the email." >&2
    exit 1
  fi

  local payload
  payload="$(python3 "$EVALUATOR" email \
    --state-json "$state_json" \
    --action "$action" \
    --run-url "$run_url")" || {
    echo "::error::Could not render the audit-monitor email payload." >&2
    exit 1
  }
  local subject body
  subject="$(printf '%s' "$payload" | jq -r .subject)"
  body="$(printf '%s' "$payload" | jq -r .body)"

  if [[ "${DRY_RUN:-}" = "1" ]]; then
    # Local proof / test seam: show exactly what would be sent, send nothing.
    printf '%s' "$payload" | jq -c --arg action "$action" '{action: $action, subject: .subject, body: .body}'
    exit 0
  fi

  if [[ -z "${ZOHO_SMTP_USER:-}" ]] || [[ -z "${ZOHO_SMTP_PASS:-}" ]] || [[ -z "${ISSUE_NOTIFY_EMAIL:-}" ]]; then
    # Best-effort channel: unconfigured is reported, never silently green. The
    # Linear ticket leg still fires.
    echo "::warning::Audit-monitor transition ($action) but ZOHO_SMTP_USER / ZOHO_SMTP_PASS / ISSUE_NOTIFY_EMAIL are not configured - NO email was sent. Configure the same secrets issue-notify.yml uses."
    exit 0
  fi

  SUBJECT="$subject" BODY="$body" python3 - <<'PY'
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.utils import formatdate

HOST = "smtppro.zoho.eu"
PORT_STARTTLS = 587
PORT_SSL = 465

user = os.environ["ZOHO_SMTP_USER"]
password = os.environ["ZOHO_SMTP_PASS"]
to_addr = os.environ["ISSUE_NOTIFY_EMAIL"]
from_addr = os.environ.get("ISSUE_NOTIFY_FROM") or user
subject = os.environ["SUBJECT"]
body = os.environ["BODY"]


def scrub(text: str) -> str:
    """Never let the SMTP password appear in any log output."""
    return text.replace(password, "***") if password else text


message = MIMEText(body + "\n")
message["Subject"] = subject
message["From"] = from_addr
message["To"] = to_addr
message["Date"] = formatdate(localtime=True)


def send(port: int, use_ssl: bool) -> None:
    if use_ssl:
        client = smtplib.SMTP_SSL(HOST, port, timeout=30)
    else:
        client = smtplib.SMTP(HOST, port, timeout=30)
    try:
        if not use_ssl:
            client.starttls()
        client.login(user, password)
        client.sendmail(from_addr, [to_addr], message.as_string())
    finally:
        try:
            client.quit()
        except Exception:
            pass


try:
    send(PORT_STARTTLS, use_ssl=False)
except Exception as first_error:
    try:
        send(PORT_SSL, use_ssl=True)
    except Exception as second_error:
        print(
            scrub(f"SMTP send failed on 587 ({first_error}) and 465 ({second_error})"),
            file=sys.stderr,
        )
        sys.exit(1)

print(scrub(f"Sent '{subject}' to {to_addr}"))
PY
}

case "$MODE" in
  evaluate) run_evaluate ;;
  email) run_email ;;
  *)
    echo "::error::unknown mode '$MODE' (expected evaluate or email)" >&2
    exit 2
    ;;
esac
