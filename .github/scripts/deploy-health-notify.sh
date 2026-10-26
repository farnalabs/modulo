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
# VERDICT_STEP_NAME step (FAILED = unhealthy, SKIPPED = not unhealthy) in the
# most recent completed run that MEASURED something. No state store is
# introduced. An unreadable / missing previous verdict is UNKNOWN, which never
# suppresses an alert.
#
# FAR-2157: a tick whose evaluation is UNKNOWN runs neither the verdict step
# nor a healthy verdict -- it runs the workflow's UNKNOWN-tick marker step,
# which `deploy_health.parse_previous_verdict` reads as UNKNOWN. The lookup
# therefore walks BACKWARDS past such ticks to the last measured verdict
# (bounded), instead of stopping at the first completed run: stopping there
# recorded an unmeasured tick as healthy (its verdict step is skipped), which
# silently swallowed the recovery email between an escalation and its
# recovery. A lookup that cannot be read is skipped, never trusted.
#
# FAR-2159: a tick that read only SOME of its required inputs is UNKNOWN too
# (`deploy_health._overall_healthy`), so it takes THIS same path -- no email is
# sent and a live alert stays open. That is deliberate: a recovery must never
# be inferred from figures the tick did not read.
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
UNKNOWN_VERDICT="unknown"
export UNKNOWN_VERDICT
RUN_URL="${RUN_URL:-}"
DEPLOY_URL="${DEPLOY_URL:-}"

emit_output() {
  # GITHUB_OUTPUT is absent when the script runs locally.
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s\n' "$1=$2" >> "$GITHUB_OUTPUT"
  fi
  return 0
}

# A missing evaluator must never degrade to "no transition": the transition
# decision would silently report none and the email edge would switch itself
# off without a signal.
if [[ ! -f "$EVALUATOR" ]]; then
  echo "::error::The shared deploy-health evaluator is missing ($EVALUATOR) - the deploy-health email edge cannot run." >&2
  exit 1
fi

# ---- 0. An evaluation that produced no verdict cannot infer a transition. ----
# Both UNKNOWN shapes land here: a tick that measured nothing (FAR-2157) and a
# PARTIAL read that missed a required input (FAR-2159). Neither may close an
# alert, so neither sends anything.
if [[ -z "$CURRENT_HEALTHY" ]]; then
  echo "::warning::Deploy health could not be evaluated this cycle (UNKNOWN - nothing was readable, or the tick was a partial read missing a required input) - no transition can be inferred, so no email is sent. A live alert therefore stays open. The run still warns; an UNKNOWN previous verdict never suppresses the next alert."
  emit_output action none
  exit 0
fi

# ---- 1. Previous verdict: this workflow's most recent completed run. ----
PREV_VERDICT="${PREVIOUS_VERDICT:-}"
if [[ -z "$PREV_VERDICT" ]]; then
  PREV_VERDICT="$UNKNOWN_VERDICT"
  if [[ -n "${REPOSITORY:-}" ]] && [[ -n "$WORKFLOW_NAME" ]] && command -v gh >/dev/null 2>&1; then
    PREV_RUNS="$(gh run list --repo "$REPOSITORY" --workflow "$WORKFLOW_NAME" --limit 10 \
      --json databaseId,status,conclusion,createdAt 2>/dev/null)" || PREV_RUNS=""
    if [[ -n "$PREV_RUNS" ]]; then
      # Newest-first ids of completed runs other than this one, one per line.
      PREV_IDS="$(PAYLOAD="$PREV_RUNS" CURRENT="${GITHUB_RUN_ID:-}" python3 - <<'PY'
import json
import os

try:
    runs = json.loads(os.environ["PAYLOAD"])
except Exception:
    runs = []
current = str(os.environ.get("CURRENT", ""))
for run in runs:
    if run.get("status") == "completed" and str(run.get("databaseId")) != current:
        print(run["databaseId"])
PY
)" || PREV_IDS=""
      if [[ -n "$PREV_IDS" ]]; then
        # FAR-2157: walk back to the last MEASURED verdict. A tick that measured
        # nothing carries the UNKNOWN-tick marker, so the evaluator returns
        # UNKNOWN for it; such a run must neither be trusted as healthy nor
        # stop the walk. (A run from before the marker existed cannot be told
        # apart from a healthy one - it is read as healthy, as it always was.)
        # Bounded: at most LOOKUP_BUDGET job reads.
        LOOKUP_BUDGET=5
        LOOKED_UP=0
        while IFS= read -r PREV_ID; do
          [[ -z "$PREV_ID" ]] && continue
          if [[ "$LOOKED_UP" -ge "$LOOKUP_BUDGET" ]]; then
            echo "::warning::Previous-verdict walk stopped after ${LOOKUP_BUDGET} unmeasured run(s) - treating the previous verdict as UNKNOWN, which alerts rather than suppresses."
            break
          fi
          LOOKED_UP=$((LOOKED_UP + 1))
          PREV_JOBS_FILE="$(mktemp)"
          if gh api "repos/$REPOSITORY/actions/runs/$PREV_ID/jobs" > "$PREV_JOBS_FILE" 2>/dev/null; then
            RUN_VERDICT="$(python3 "$EVALUATOR" previous-verdict --jobs-json "$PREV_JOBS_FILE")" || RUN_VERDICT="$UNKNOWN_VERDICT"
            rm -f "$PREV_JOBS_FILE"
            if [[ "$RUN_VERDICT" != "$UNKNOWN_VERDICT" ]]; then
              PREV_VERDICT="$RUN_VERDICT"
              echo "Previous verdict read from run $PREV_ID."
              break
            fi
            echo "Run $PREV_ID measured nothing (UNKNOWN) - walking back to the last measured verdict."
          else
            rm -f "$PREV_JOBS_FILE"
            echo "::warning::Could not read run ${PREV_ID}'s jobs - skipping it rather than trusting it."
          fi
        done <<< "$PREV_IDS"
        if [[ "$PREV_VERDICT" = "$UNKNOWN_VERDICT" ]]; then
          echo "::warning::No measured previous verdict found in this workflow's recent runs - treating it as UNKNOWN, which alerts rather than suppresses."
        fi
      else
        echo "No completed previous run of this workflow - treating the previous verdict as UNKNOWN, which alerts rather than suppresses."
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
if [[ "$ACTION" = "none" ]]; then
  echo "No transition (current healthy=$CURRENT_HEALTHY, previous=$PREV_VERDICT) - nothing to send."
  exit 0
fi

# ---- 3. Render the email with the shared renderer BEFORE checking SMTP. ----
if [[ -z "$VERDICT_JSON" ]] || [[ ! -f "$VERDICT_JSON" ]]; then
  echo "::error::Deploy-health transition ($ACTION) but the verdict JSON is missing ($VERDICT_JSON) - cannot render the email." >&2
  exit 1
fi
PAYLOAD="$(python3 "$EVALUATOR" email \
  --verdict-json "$VERDICT_JSON" \
  --action "$ACTION" \
  --run-url "$RUN_URL" \
  --deploy-url "$DEPLOY_URL")" || {
  echo "::error::Could not render the deploy-health email payload." >&2
  exit 1
}
SUBJECT="$(printf '%s' "$PAYLOAD" | jq -r .subject)"
BODY="$(printf '%s' "$PAYLOAD" | jq -r .body)"

if [[ "${DRY_RUN:-}" = "1" ]]; then
  # Local proof / CI test seam: show exactly what would be sent, send nothing.
  printf '%s' "$PAYLOAD" | jq -c '{action: "'"$ACTION"'", subject: .subject, body: .body}'
  exit 0
fi

if [[ -z "${ZOHO_SMTP_USER:-}" ]] || [[ -z "${ZOHO_SMTP_PASS:-}" ]] || [[ -z "${ISSUE_NOTIFY_EMAIL:-}" ]]; then
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
STARTTLS_LABEL = f"{PORT_STARTTLS}"
SSL_LABEL = f"{PORT_SSL}"

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
            scrub(
                f"SMTP send failed on {STARTTLS_LABEL} ({first_error}) "
                f"and {SSL_LABEL} ({second_error})"
            ),
            file=sys.stderr,
        )
        sys.exit(1)

print(scrub(f"Sent '{subject}' to {to_addr}"))
PY
