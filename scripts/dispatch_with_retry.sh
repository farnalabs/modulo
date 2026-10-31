#!/usr/bin/env bash
# Shared webhook-dispatch retry policy for the Modulo app's webhook endpoints
# (Branch Fixer, PR Reviewer). Every workflow step that POSTs to
# MODULO_*_WEBHOOK_URL calls this script instead of inlining its own retry
# loop, so the policy has exactly ONE definition (ci.yml inlined the same
# policy per PR #2120's fix while its tests pin that copy's two in-workflow
# sites byte-identically; this script is the migration target — when PR
# #2120's copy is folded into callers of this script, that structure test
# should flip to asserting the script is the single definition).
#
# CLASSIFICATION (same as PR #2120's Branch Fixer dispatch fix):
# - HTTP 429 = capacity saturation (its own class): the trigger's concurrent
#   run slots are full. Retryable — an operator drains a backlog.
# - HTTP 000 (curl transport failure, no response at all), 502/503/504 =
#   transient reachability. Retryable.
# - Every other non-2xx (401, 400 other than the dup guard, deterministic
#   500/501, ...) = TERMINAL and fails immediately: retrying only delays the
#   loud failure annotation.
# - Success: HTTP 202 by default. DISPATCH_ACCEPT_ANY_2XX=1 widens success to
#   any 2xx (the PR Reviewer webhook contract, which the two caller sites of
#   that trigger accept any 2xx on). Under the default 202-only contract a
#   non-202 2xx is terminal: something other than the app answered, and the
#   dispatch must not be recorded as delivered.
# - HTTP 400 with "Duplicate webhook payload" in the body = the engine's
#   byte-level idempotency guard (DuplicateWebhookError, FAR-1097 follow-up):
#   the identical delivery was already accepted within the ~5-min dedup
#   window. A handled no-op, not a failure (DISPATCH_DUP_400_IS_OK=1 default).
#
# BOUNDED. Retrying is bounded by BOTH an attempt cap (8) and a wall-clock
# budget (300s). A dispatch that never gives up holds the calling job open
# indefinitely and can outlive the PR's head SHA, so its eventual delivery
# would already be stale.
#
# The numbers are chosen so the WALL-CLOCK BUDGET is the binding constraint and
# the attempt cap is only a backstop. Scheduled waits are 10s, 20s, 40s then
# 60s for every later attempt (plus <= 5s jitter): six sleeps put ~250-280s of
# wall clock on the job, and the wait after the seventh attempt no longer fits
# the 300s budget, so a saturated trigger gets ~7 attempts spread over ~4 minutes
# instead of a burst. The cost is bounded by the same budget: a dispatch job
# can hold a runner for at most 300s of waiting plus the per-attempt curl time
# (10s connect / 30s total each). The attempt cap of 8 stays above the number
# the budget permits, so it can never truncate a budget-bound run early — it
# only guards callers that override DISPATCH_BUDGET_SECONDS upward.
#
# Retry-After (RFC 9110 delta-seconds or HTTP-date) is a FLOOR on when the
# server may next accept, never the retry schedule: the delay is the LARGER of
# the server's value and this attempt's exponential backoff (base 10s,
# per-attempt cap 60s) plus bounded jitter (<= 5s) so many failing jobs do not
# retry in lockstep and re-saturate the same slots. Honouring a SHORT
# Retry-After verbatim defeats the budget: the Branch Fixer's 429 answers
# `Retry-After: 5`, so every attempt would wait 5s, the attempt cap would be
# spent in ~35s of sleep, and DISPATCH_BUDGET_SECONDS would never bind
# (observed on PR #2165: 8 attempts in 38s, all HTTP 429, CI failure never
# handed to the fix loop). A Retry-After LONGER than the remaining budget is
# still honoured as an instruction not to retry early: the loop abandons with
# the budget warning rather than retrying inside a window the server closed.
#
# Failures are NEVER silent: on a failed dispatch the script emits a class-
# named ::error:: annotation, writes a durable GITHUB_STEP_SUMMARY block, and
# exits 1 so the calling workflow job fails visibly.
#
# Usage (from a step whose env carries the secret/config values):
#   BODY=$(jq -n ...)          # build the payload as today
#   DISPATCH_BODY="${BODY}" bash scripts/dispatch_with_retry.sh
#
# Environment:
#   DISPATCH_URL            (required) webhook endpoint.
#   DISPATCH_BODY           (required) JSON payload string.
#   DISPATCH_SECRET         (required) HMAC secret for X-Modulo-Webhook-Secret.
#   DISPATCH_LABEL          log prefix, e.g. "Modulo Branch Fixer".
#   DISPATCH_ACCEPT_ANY_2XX "1" accepts any 2xx (default: 202-only).
#   DISPATCH_DUP_400_IS_OK  "1" treats 400 "Duplicate webhook payload" as a
#                           handled coalesced no-op (default).
#   DISPATCH_RESULT_FILE    if set, receives machine-readable
#                           DISPATCH_LAST_HTTP / _CLASS / _DETAIL / ATTEMPTS /
#                           _ELAPSED key=value lines (bash %q-quoted) so a
#                           caller can compose failure comments (the PR
#                           conflict reconciler's durable PR-comment block).
#   DISPATCH_BUDGET_SECONDS / DISPATCH_MAX_ATTEMPTS / DISPATCH_BASE_DELAY /
#   DISPATCH_MAX_DELAY / DISPATCH_JITTER_MAX — overridable so tests can
#   exercise budget exhaustion without waiting real minutes.
#
# Requires only bash, curl, jq, openssl, date, grep, cut, sed, tr — the tools
# every caller site already uses. No actions/checkout dependency beyond the
# script itself being checked out in the calling workflow.

set -euo pipefail

# A missing URL or secret is a CONFIGURATION failure, and the usual cause is
# that GitHub withholds Actions secrets from `dependabot[bot]` PRs (they need
# Dependabot secrets). Hand the caller a result file naming it, so the PR
# comment says "secret missing" rather than "HTTP unknown, check the HMAC
# secret". Same exit-1 contract as the `:?` checks below.
for REQUIRED_VAR in DISPATCH_URL DISPATCH_SECRET; do
  if [[ -z "${!REQUIRED_VAR:-}" ]]; then
    echo "dispatch_with_retry: ${REQUIRED_VAR} is required" >&2
    echo "::error::${DISPATCH_LABEL:-webhook} dispatch not attempted: ${REQUIRED_VAR} is empty (secret not available to this run, e.g. a dependabot PR)."
    if [[ -n "${DISPATCH_RESULT_FILE:-}" ]]; then
      {
        printf 'DISPATCH_LAST_HTTP=%q\n' "none"
        printf 'DISPATCH_LAST_CLASS=%q\n' "config"
        printf 'DISPATCH_LAST_DETAIL=%q\n' "${REQUIRED_VAR} is empty"
        printf 'DISPATCH_ATTEMPTS=%q\n' "0"
        printf 'DISPATCH_BUDGET_ELAPSED=%q\n' "0"
        printf 'DISPATCH_DELIVERED=%q\n' "false"
      } > "${DISPATCH_RESULT_FILE}"
    fi
    exit 1
  fi
done
DISPATCH_BODY="${DISPATCH_BODY:?dispatch_with_retry: DISPATCH_BODY is required}"
DISPATCH_LABEL="${DISPATCH_LABEL:-webhook}"
DISPATCH_ACCEPT_ANY_2XX="${DISPATCH_ACCEPT_ANY_2XX:-0}"
DISPATCH_DUP_400_IS_OK="${DISPATCH_DUP_400_IS_OK:-1}"
DISPATCH_BUDGET_SECONDS="${DISPATCH_BUDGET_SECONDS:-300}"
DISPATCH_MAX_ATTEMPTS="${DISPATCH_MAX_ATTEMPTS:-8}"
DISPATCH_BASE_DELAY="${DISPATCH_BASE_DELAY:-10}"
DISPATCH_MAX_DELAY="${DISPATCH_MAX_DELAY:-60}"
DISPATCH_JITTER_MAX="${DISPATCH_JITTER_MAX:-5}"
DISPATCH_RESULT_FILE="${DISPATCH_RESULT_FILE:-}"

classify_http() {
  local http_code="$1"
  # Retryable (capacity|transient) vs terminal. Acceptance (202 / any-2xx)
  # is handled by the caller before this is invoked — but a non-202 2xx under
  # the 202-only contract falls through as terminal: the webhook contract is
  # 202, so a bare 200 means something other than the app answered and the
  # dispatch must not be recorded as delivered.
  case "$http_code" in
    429) printf 'capacity\n' ;;
    000|502|503|504) printf 'transient\n' ;;
    *) printf 'terminal\n' ;;
  esac
}

parse_retry_after() {
  local retry_after="${1-}"
  # Whole seconds from a Retry-After value, or empty when absent or
  # unparseable. Never emits a negative value.
  case "$retry_after" in
    '') return 0 ;;
    *[!0-9]*) ;;
    *) printf '%s\n' "$retry_after"; return 0 ;;
  esac
  local retry_epoch
  local retry_now
  retry_epoch=$(date -u -d "$retry_after" +%s 2>/dev/null) || return 0
  retry_now=$(date +%s)
  if [[ "$retry_epoch" -gt "$retry_now" ]]; then
    printf '%s\n' "$((retry_epoch - retry_now))"
  fi
}

retry_delay() {
  local attempt="${1:?attempt number is required}"
  local retry_after="${2-}"
  local backoff
  # Retry-After is a FLOOR, not the schedule: the delay is the larger of the
  # server's value and this attempt's exponential backoff (clamped to
  # DISPATCH_MAX_DELAY, jitter added on top). A short Retry-After therefore
  # cannot collapse the backoff into a tight loop that exhausts the attempt cap
  # before the wall-clock budget can bind (see the header).
  backoff=$(( DISPATCH_BASE_DELAY * (1 << (attempt - 1)) ))
  if [[ "$backoff" -gt "$DISPATCH_MAX_DELAY" ]]; then backoff="$DISPATCH_MAX_DELAY"; fi
  backoff=$(( backoff + RANDOM % (DISPATCH_JITTER_MAX + 1) ))
  # Annotated assignment keeps `set -e` from treating an empty/non-numeric
  # value as a failed arithmetic expansion; parse_retry_after only ever emits
  # a non-negative whole number, so `10#` is safe.
  if [[ -n "$retry_after" ]] && [[ "$((10#$retry_after))" -gt "$backoff" ]]; then
    printf '%s\n' "$retry_after"
    return 0
  fi
  printf '%s\n' "$backoff"
}

HEADER_FILE=$(mktemp)
BODY_FILE=$(mktemp)
trap 'rm -f "${HEADER_FILE}" "${BODY_FILE}"' EXIT

DISPATCHED="false"
DISPATCH_STARTED=$(date +%s)
ATTEMPTS=0
LAST_HTTP=""
LAST_CLASS=""
LAST_DETAIL="unknown"

while [[ "$ATTEMPTS" -lt "$DISPATCH_MAX_ATTEMPTS" ]]; do
  ATTEMPTS=$((ATTEMPTS + 1))
  # TIMESTAMP and HMAC are recomputed on every attempt: the server enforces a
  # +/-300s replay window on X-Modulo-Timestamp, so a signature computed once
  # outside the loop would fall outside the window after a long backoff.
  TIMESTAMP=$(date +%s)
  HMAC=$(printf '%s' "${TIMESTAMP}.${DISPATCH_BODY}" | openssl dgst -sha256 -hmac "${DISPATCH_SECRET}" | sed 's/^.* //')
  # Truncate the per-attempt files so a curl failure that writes nothing
  # cannot surface the previous attempt's stale body/headers (control flow
  # keys off HTTP_CODE, but the echoed body must stay honest).
  : > "${HEADER_FILE}"
  : > "${BODY_FILE}"
  HTTP_CODE=$(curl -s --connect-timeout 10 --max-time 30 \
    -D "${HEADER_FILE}" -o "${BODY_FILE}" -w '%{http_code}' \
    -X POST "${DISPATCH_URL}" \
    -H "Content-Type: application/json" \
    -H "X-Modulo-Timestamp: ${TIMESTAMP}" \
    -H "X-Modulo-Webhook-Secret: sha256=${HMAC}" \
    -d "${DISPATCH_BODY}") || HTTP_CODE="000"
  [[ -n "$HTTP_CODE" ]] || HTTP_CODE="000"
  BODY_RESP=$(cat "${BODY_FILE}" 2>/dev/null || true)
  LAST_HTTP="$HTTP_CODE"

  # ---- acceptance ----
  # Check for 2xx success first
  is_2xx=false
  if [[ "${HTTP_CODE:0:1}" == "2" ]]; then
    is_2xx=true
  fi

  if [[ "$is_2xx" == "true" ]]; then
    # 202-only by default; DISPATCH_ACCEPT_ANY_2XX=1 widens to any 2xx
    accepts_this_2xx=false
    if [[ "$HTTP_CODE" == "202" ]] || [[ "$DISPATCH_ACCEPT_ANY_2XX" == "1" ]]; then
      accepts_this_2xx=true
    fi

    if [[ "$accepts_this_2xx" == "true" ]]; then
      RESP_STATUS=$(printf '%s' "$BODY_RESP" | jq -r '.status // "accepted"' 2>/dev/null || printf 'accepted')
      if [[ "$RESP_STATUS" == "coalesced" ]]; then
        # FAR-1034 handled no-op: duplicate delivery for the same key
        # within the coalesce window — intentionally no new run. Not an
        # error, not retried.
        echo "::notice::${DISPATCH_LABEL} coalesced (duplicate delivery for the same key — suppressed)"
      else
        echo "${DISPATCH_LABEL} accepted (run: $(printf '%s' "$BODY_RESP" | jq -r '.run_id // "unknown"' 2>/dev/null || printf 'unknown'))"
      fi
      DISPATCHED="true"
      break
    fi
  fi

  # Check for 400 duplicate payload (handled no-op)
  is_400_dup=false
  if [[ "$HTTP_CODE" == "400" ]] && [[ "$DISPATCH_DUP_400_IS_OK" == "1" ]]; then
    if printf '%s' "$BODY_RESP" | grep -q "Duplicate webhook payload"; then
      is_400_dup=true
    fi
  fi

  if [[ "$is_400_dup" == "true" ]]; then
    # FAR-1097 follow-up: HTTP 400 "Duplicate webhook payload" is the
    # engine's byte-level idempotency guard (DuplicateWebhookError) — the
    # identical payload was already ACCEPTED within the ~5-min dedup window
    # (a concurrent run or a workflow re-run). Retrying would only re-hit
    # the same window.
    echo "::notice::${DISPATCH_LABEL} duplicate delivery suppressed (identical payload already accepted within the dedup window)"
    DISPATCHED="true"
    break
  fi

  # ---- classification for the retry decision ----
  LAST_CLASS=$(classify_http "$HTTP_CODE")
  LAST_DETAIL=$(printf '%s' "$BODY_RESP" | jq -r '.detail // "unknown"' 2>/dev/null || printf 'unknown')
  [[ -n "$LAST_DETAIL" ]] || LAST_DETAIL="unknown"

  if [[ "$LAST_CLASS" == "terminal" ]]; then
    echo "::warning::${DISPATCH_LABEL} returned HTTP ${HTTP_CODE} (non-retryable): ${LAST_DETAIL}"
    break
  fi

  DISPATCH_ELAPSED=$(( $(date +%s) - DISPATCH_STARTED ))
  DISPATCH_REMAINING=$(( DISPATCH_BUDGET_SECONDS - DISPATCH_ELAPSED ))

  # Check attempt cap
  if [[ "$ATTEMPTS" -ge "$DISPATCH_MAX_ATTEMPTS" ]]; then
    echo "::warning::${DISPATCH_LABEL} returned HTTP ${HTTP_CODE} (${LAST_CLASS}) on attempt ${ATTEMPTS}/${DISPATCH_MAX_ATTEMPTS} — attempt cap reached"
    break
  fi

  # Check budget exhaustion
  if [[ "$DISPATCH_REMAINING" -le 0 ]]; then
    echo "::warning::${DISPATCH_LABEL} returned HTTP ${HTTP_CODE} (${LAST_CLASS}) — ${DISPATCH_BUDGET_SECONDS}s dispatch budget exhausted after ${DISPATCH_ELAPSED}s"
    break
  fi

  RETRY_AFTER=$(grep -i '^retry-after:' "${HEADER_FILE}" 2>/dev/null | tail -1 | cut -d: -f2- | tr -d '\r' | sed 's/^[[:space:]]*//' || true)
  DISPATCH_DELAY=$(retry_delay "$ATTEMPTS" "$(parse_retry_after "${RETRY_AFTER}")")

  # Check if delay exceeds remaining budget
  if [[ "$DISPATCH_DELAY" -gt "$DISPATCH_REMAINING" ]]; then
    echo "::warning::${DISPATCH_LABEL} returned HTTP ${HTTP_CODE} (${LAST_CLASS}); the next attempt needs ${DISPATCH_DELAY}s but only ${DISPATCH_REMAINING}s of the ${DISPATCH_BUDGET_SECONDS}s budget remain — not waiting"
    break
  fi

  echo "::warning::${DISPATCH_LABEL} returned HTTP ${HTTP_CODE} (${LAST_CLASS}, attempt ${ATTEMPTS}/${DISPATCH_MAX_ATTEMPTS}) — retrying in ${DISPATCH_DELAY}s"
  sleep "$DISPATCH_DELAY"
done

# Invoked indirectly via `trap ... EXIT` — shellcheck cannot see that call.
# shellcheck disable=SC2317
write_result_file() {
  if [[ -n "$DISPATCH_RESULT_FILE" ]]; then
    {
      printf 'DISPATCH_LAST_HTTP=%q\n' "$LAST_HTTP"
      printf 'DISPATCH_LAST_CLASS=%q\n' "$LAST_CLASS"
      printf 'DISPATCH_LAST_DETAIL=%q\n' "$LAST_DETAIL"
      printf 'DISPATCH_ATTEMPTS=%q\n' "$ATTEMPTS"
      printf 'DISPATCH_BUDGET_ELAPSED=%q\n' "$(( $(date +%s) - DISPATCH_STARTED ))"
      printf 'DISPATCH_DELIVERED=%q\n' "$DISPATCHED"
    } > "${DISPATCH_RESULT_FILE}"
  fi
}
trap 'write_result_file; rm -f "${HEADER_FILE}" "${BODY_FILE}"' EXIT

if [[ "$DISPATCHED" != "true" ]]; then
  DISPATCH_ELAPSED=$(( $(date +%s) - DISPATCH_STARTED ))
  # Name the CLASS of failure, not just its HTTP code: capacity saturation
  # is an operational backlog an operator drains, while a terminal error is
  # a broken request or credential nobody is waiting on. The two need
  # different actions, so the annotation and the run summary carry the
  # distinction.
  case "$LAST_CLASS" in
    capacity)
      DISPATCH_FAILURE="capacity saturated"
      DISPATCH_CAUSE="the ${DISPATCH_LABEL} trigger is at its concurrent-run limit (HTTP ${LAST_HTTP}), so the webhook was refused because the app is busy - not because anything is broken"
      DISPATCH_ACTION="Drain in-flight runs for this trigger (or raise the trigger's \`max_concurrent_runs\`), then re-run this workflow job to hand the failure over."
      ;;
    transient)
      DISPATCH_FAILURE="webhook unreachable"
      DISPATCH_CAUSE="the ${DISPATCH_LABEL} webhook or a gateway in front of it was unavailable (HTTP ${LAST_HTTP}) for the whole retry budget"
      DISPATCH_ACTION="Check that app.modulo.run is healthy and that the webhook secret points at it, then re-run this workflow job."
      ;;
    *)
      DISPATCH_FAILURE="non-retryable rejection"
      DISPATCH_CAUSE="the ${DISPATCH_LABEL} webhook rejected the request outright (HTTP ${LAST_HTTP}) - retrying cannot help"
      DISPATCH_ACTION="Check the webhook HMAC secret and the payload; this needs a code or configuration fix, not a retry."
      ;;
  esac
  echo "::error::${DISPATCH_LABEL} dispatch failed — ${DISPATCH_FAILURE} after ${ATTEMPTS} attempt(s) in ${DISPATCH_ELAPSED}s (last HTTP ${LAST_HTTP}: ${LAST_DETAIL})."
  # A durable trace that survives the log window: the summary is attached to
  # the failed job itself, so "the fixer/reviewer was never dispatched" is
  # readable without grepping a log that may be gone.
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo "### ${DISPATCH_LABEL} dispatch failed: ${DISPATCH_FAILURE}"
      echo ""
      echo "- Attempts: ${ATTEMPTS} in ${DISPATCH_ELAPSED}s (budget ${DISPATCH_BUDGET_SECONDS}s, cap ${DISPATCH_MAX_ATTEMPTS})"
      echo "- Last HTTP: \`${LAST_HTTP}\` — ${LAST_DETAIL}"
      echo "- Why: ${DISPATCH_CAUSE}"
      echo "- Next step: ${DISPATCH_ACTION}"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 1
fi

echo "${DISPATCH_LABEL} dispatch delivered (HTTP ${LAST_HTTP})."
exit 0
