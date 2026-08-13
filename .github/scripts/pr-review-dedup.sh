#!/usr/bin/env bash
# pr-review-dedup.sh
#
# Decide whether the PR Reviewer webhook can be skipped for the current head of
# a PR. Runs on the trigger side (ci.yml "Skip PR Reviewer when head already
# reviewed or only merged since last review") and mirrors the reviewer-side
# contract so a green CI run does not re-pay the full agent re-review cost when
# nothing about the code changed.
#
# Fail-open by design: every uncertain path dispatches (skip=false). We only
# skip when we are confident the current head was already formally reviewed and
# the code has not changed since (same SHA, or a merge-only head move under a
# decisive prior review).
#
# Inputs (environment):
#   REVIEWS_JSON   GitHub pulls/{n}/reviews JSON array ("[]" on API error)
#   COMMITS_JSON   GitHub pulls/{n}/commits JSON array ("[]" on API error)
#   HEAD_SHA       pull_request.head.sha of the current event
#
# Outputs (stdout, GITHUB_OUTPUT compatible):
#   skip=true|false
#   reason=<single-line human-readable explanation>
#
# Exit codes: 0 = decision emitted, 2 = usage error.

set -euo pipefail

REVIEWS_JSON="${REVIEWS_JSON:-}"
COMMITS_JSON="${COMMITS_JSON:-}"
HEAD_SHA="${HEAD_SHA:-}"

if [ -z "$REVIEWS_JSON" ] || [ -z "$COMMITS_JSON" ] || [ -z "$HEAD_SHA" ]; then
  echo "usage: REVIEWS_JSON=<reviews> COMMITS_JSON=<commits> HEAD_SHA=<sha> $0" >&2
  exit 2
fi

emit() {
  printf 'skip=%s\n' "$1"
  printf 'reason=%s\n' "$2"
}

# 1. No reviews at all (or the reviews API call failed and the caller passed
#    "[]"). Dispatch - this is a first review.
if [ "$REVIEWS_JSON" = "[]" ] || [ "$REVIEWS_JSON" = "null" ]; then
  emit false "No prior review on PR - dispatching first review."
  exit 0
fi

# 2. Find the most recent non-PENDING review. The API returns reviews in
#    chronological order, so `last` is the latest. Extract each field with its
#    OWN jq call - review bodies are free-form and routinely contain newlines
#    and pipes, so joining fields with '|' and splitting with `cut` corrupts
#    the SHA/state for any multi-line body (2026-08-13 review finding).
LAST_REVIEW=$(printf '%s' "$REVIEWS_JSON" | jq -c '[.[] | select(.state != "PENDING")] | last // empty' 2>/dev/null || true)

if [ -z "$LAST_REVIEW" ] || [ "$LAST_REVIEW" = "null" ]; then
  emit false "No non-PENDING prior review on PR - dispatching."
  exit 0
fi

LAST_SHA=$(printf '%s' "$LAST_REVIEW" | jq -r '.commit_id // empty' 2>/dev/null || true)
LAST_STATE=$(printf '%s' "$LAST_REVIEW" | jq -r '.state // empty' 2>/dev/null || true)
LAST_BODY=$(printf '%s' "$LAST_REVIEW" | jq -r '.body // empty' 2>/dev/null || true)

if [ -z "$LAST_SHA" ]; then
  emit false "Last review has no commit SHA - dispatching."
  exit 0
fi

# 3. Same-SHA guard: the head was already reviewed.
if [ "$LAST_SHA" = "$HEAD_SHA" ]; then
  emit true "Head ${HEAD_SHA} already reviewed - skipping PR Reviewer webhook."
  exit 0
fi

# 4. Count non-merge commits between the last review and the current head.
#    Walk the commit list (reverse-chronological) from the head; each
#    single-parent commit advances the counter until the reviewed SHA is found.
RESULT=$(printf '%s' "$COMMITS_JSON" | jq -r --arg last "$LAST_SHA" '
  (reverse | . as $cs | reduce $cs[] as $c ({n:0, found:false};
    if .found then . elif $c.sha == $last then .found = true
    elif (($c.parents | length) == 1) then .n += 1 else . end)) | "\(.found) \(.n)"' 2>/dev/null || echo "false 1")
FOUND=${RESULT%% *}
NON_MERGE_SINCE=${RESULT##* }

if [ "$FOUND" != "true" ]; then
  emit false "Last review SHA ${LAST_SHA} not found in current history (force-push?) - dispatching (fail open)."
  exit 0
fi

if [ "${NON_MERGE_SINCE:-1}" -gt 0 ]; then
  emit false "${NON_MERGE_SINCE} non-merge commit(s) since last review ${LAST_SHA} - dispatching."
  exit 0
fi

# 5. Zero non-merge commits since the last review: merge-only head move.
#    Skip only when the prior review is decisive:
#    - APPROVED: a mainline merge does not change the code, the approval stands.
#    - CHANGES_REQUESTED without a merge-conflict marker: the requested changes
#      are still outstanding and the merge did not address them.
#    Fail open (dispatch) for:
#    - CHANGES_REQUESTED mentioning "merge conflict": the merge may have just
#      resolved it, so it needs a fresh formal decision.
#    - COMMENTED / DISMISSED / anything else: a non-decisive prior review - the
#      reviewer-side contract fails open and performs a fresh review, so the
#      trigger side must dispatch to match.
case "$LAST_STATE" in
  APPROVED)
    emit true "Only merge commits since last review ${LAST_SHA} and prior review APPROVED - skipping PR Reviewer webhook."
    exit 0
    ;;
  CHANGES_REQUESTED)
    if printf '%s' "$LAST_BODY" | grep -qi "merge conflict"; then
      emit false "Prior review was a merge-conflict CHANGES_REQUESTED - merge may resolve it - dispatching."
    else
      emit true "Only merge commits since last review ${LAST_SHA} and prior review CHANGES_REQUESTED (no merge-conflict marker) - skipping PR Reviewer webhook."
    fi
    exit 0
    ;;
  *)
    emit false "Merge-only head move with non-decisive prior review (${LAST_STATE:-none}) - dispatching (fail open)."
    exit 0
    ;;
esac
