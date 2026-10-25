#!/usr/bin/env bash
# Shared deploy-health EMAIL transition step (FAR-2116, FAR-2139).
#
# ONE implementation for every workflow that escalates deploy health:
#   - deploy-staleness-check.yml (app-level staleness + consecutive failures)
#   - deploy-watchdog.yml       (fly-level self-heal exhausted / dispatch failed)
#
# The two workflows measure different things, so only the *evaluation* lives
# per workflow; the previous-verdict lookup, the transition decision and the
# email itself live here so they cannot drift into two formats. The email is
# rendered by `.github/scripts/deploy_health.py email`, the same renderer the
# staleness check uses.
#
# State vector: the previous verdict is the conclusion of this workflow's
# VERDICT_STEP_NAME step in its most recent completed run -- FAILED when that
# run evaluated unhealthy, SKIPPED when it did not. No state store is
# introduced. An unreadable / missing previous verdict is UNKNOWN, which never
# suppresses an alert.
#
# Known limitation (fail-loud, not fail-silent): a tick whose evaluation is
# UNKNOWN skips the verdict step, so its run is indistinguishable from a
# healthy one in run history. The consequence is that an unreadable tick
# BETWEEN an escalation and its recovery can swallow the recovery email, and an
# unreadable tick between two unhealthy ticks re-sends the alert once. Both
# directions are deliberately loud; neither can hide an escalation.
#
# Inputs (all via env: -- never interpolated into the shell):
#   WORKFLOW_NAME       workflow whose run history holds the state vector
#   VERDICT_STEP_NAME   step whose conclusion encodes the previous verdict
#   CURRENT_HEALTHY     "true" | "false" | "" (UNKNOWN)
#   VERDICT_JSON        path to the Verdict JSON (deploy_health.py --verdict-out)
#   RUN_URL, DEPLOY_URL links included in the email
#   ZOHO_SMTP_USER, ZOHO_SMTP_PASS, ISSUE_NOTIFY_EMAIL, ISSUE_NOTIFY_FROM
#                       the same Zoho SMTP secrets issue-notify.yml uses
#   EVALUATOR           optional override of the evaluator path
#   DRY_RUN             "1" -> resolve + render but do not send; prints the
#                       rendered payload as JSON (used by the local proof)
#   PREVIOUS_VERDICT    optional override of the run-history lookup
#                       ("healthy" | "unhealthy" | "unknown"); the local proof
#                       uses it, workflows do not set it
#   GH_TOKEN, REPOSITORY, GITHUB_RUN_ID, GITHUB_OUTPUT -- supplied by the runner
#
# Exit code: 0 for every best-effort outcome (no transition, SMTP not
# configured, UNKNOWN health) with a ::warning:: where a human must act; 1 only
# when the email payload cannot be rendered, because that is a defect in the
# workflow rather than a transient condition.
#
# Usage in a workflow step:
#   - name: Email the operator on a deploy-health transition
#     if: always()
#     shell: bash
#     env: { ...see above... }
#     run: bash .github/scripts/deploy-health-notify.sh

set -o pipefail

EVALUATOR="${EVALUATOR:-.github/scripts/deploy_health.py}"
WORKFLOW_NAME="${WORKFLOW_NAME:-}"
VERDICT_STEP_NAME="${VERDICT_STEP_NAME:-}"
CURRENT_HEALTHY="${CURRENT_HEALTHY:-}"
VERDICT_JSON="${VERDICT_JSON:-}"
RUN_URL="${RUN_URL:-}"
DEPLOY_URL="${DEPLOY_URL:-}"

emit_output() {
  # GITHUB_OUTPUT is absent when the script runs locally.
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s\n' "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

# A missing evaluator must never degrade to "no transition": the transition
# decision would silently report none and the email edge would switch itself
# off without a signal.
if [ ! -f "$EVALUATOR" ]; then
  echo "::error::The shared deploy-health evaluator is missing ($EVALUATOR) - the deploy-health email edge cannot run."
  exit 1
fi

# ---- 0. An evaluation that produced nothing cannot infer a transition. ----
if [ -z "$CURRENT_HEALTHY" ]; then
  echo "::warning::Deploy health could not be evaluated this cycle (UNKNOWN) - no transition can be inferred, so no email is sent. The run still warns; an UNKNOWN previous verdict never suppresses the next alert."
  emit_output action none
  exit 0
fi

# ---- 1. Previous verdict: this workflow's most recent completed run. ----
PREV_VERDICT="${PREVIOUS_VERDICT:-}"
if [ -z "$PREV_VERDICT" ]; then
  PREV_VERDICT="unknown"
  if [ -n "${REPOSITORY:-}" ] && [ -n "$WORKFLOW_NAME" ] && command -v gh >/dev/null 2>&1; then
    PREV_RUNS="$(gh run list --repo "$REPOSITORY" --workflow "$WORKFLOW_NAME" --limit 10 \
      --json databaseId,status,conclusion,createdAt 2>/dev/null)" || PREV_RUNS=""
    if [ -n "$PREV_RUNS" ]; then
      PREV_ID="$(PAYLOAD="$PREV_RUNS" CURRENT="${GITHUB_RUN_ID:-}" python3 - <<'PY'
import json
import os

try:
    runs = json.loads(os.environ["PAYLOAD"])
except Exception:
    runs = []
current = str(os.environ.get("CURRENT", ""))
completed = [
    r
    for r in runs
    if r.get("status") == "completed" and str(r.get("databaseId")) != current
]
print(completed[0]["databaseId"] if completed else "")
PY
)" || PREV_ID=""
      if [ -n "$PREV_ID" ]; then
        PREV_JOBS_FILE="$(mktemp)"
        if gh api "repos/$REPOSITORY/actions/runs/$PREV_ID/jobs" > "$PREV_JOBS_FILE" 2>/dev/null; then
          PREV_VERDICT="$(python3 "$EVALUATOR" previous-verdict --jobs-json "$PREV_JOBS_FILE")" || PREV_VERDICT="unknown"
        else
          echo "::warning::Could not read the previous run's jobs ($PREV_ID) - treating the previous verdict as UNKNOWN, which alerts rather than suppresses."
        fi
        rm -f "$PREV_JOBS_FILE"
      fi
    else
      echo "::warning::Could not list this workflow's recent runs - treating the previous verdict as UNKNOWN, which alerts rather than suppresses."
    fi
  fi
fi
echo "previous verdict: $PREV_VERDICT (current healthy=$CURRENT_HEALTHY)"

# ---- 2. Transition decision (shared with the staleness check). ----
ACTION="$(python3 "$EVALUATOR" action --healthy "$CURRENT_HEALTHY" --previous "$PREV_VERDICT")" || ACTION="none"
emit_output action "$ACTION"
if [ "$ACTION" = "none" ]; then
  echo "No transition (current healthy=$CURRENT_HEALTHY, previous=$PREV_VERDICT) - nothing to send."
  exit 0
fi

# ---- 3. Render the email with the shared renderer BEFORE checking SMTP. ----
if [ -z "$VERDICT_JSON" ] || [ ! -f "$VERDICT_JSON" ]; then
  echo "::error::Deploy-health transition ($ACTION) but the verdict JSON is missing ($VERDICT_JSON) - cannot render the email."
  exit 1
fi
PAYLOAD="$(python3 "$EVALUATOR" email \
  --verdict-json "$VERDICT_JSON" \
  --action "$ACTION" \
  --run-url "$RUN_URL" \
  --deploy-url "$DEPLOY_URL")" || {
  echo "::error::Could not render the deploy-health email payload."
  exit 1
}
SUBJECT="$(printf '%s' "$PAYLOAD" | jq -r .subject)"
BODY="$(printf '%s' "$PAYLOAD" | jq -r .body)"

if [ "${DRY_RUN:-}" = "1" ]; then
  # Local proof / CI test seam: show exactly what would be sent, send nothing.
  printf '%s' "$PAYLOAD" | jq -c '{action: "'"$ACTION"'", subject: .subject, body: .body}'
  exit 0
fi

if [ -z "${ZOHO_SMTP_USER:-}" ] || [ -z "${ZOHO_SMTP_PASS:-}" ] || [ -z "${ISSUE_NOTIFY_EMAIL:-}" ]; then
  # Best-effort channel: unconfigured is reported, never silently green. The
  # red run + Linear ticket legs still fire.
  echo "::warning::Deploy-health transition ($ACTION) but ZOHO_SMTP_USER / ZOHO_SMTP_PASS / ISSUE_NOTIFY_EMAIL are not configured - NO email was sent. Configure the same secrets issue-notify.yml uses."
  exit 0
fi

# ---- 4. Send (the Zoho SMTP path issue-notify.yml already uses). ----
SUBJECT="$SUBJECT" BODY="$BODY" python3 - <<'PY'
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
