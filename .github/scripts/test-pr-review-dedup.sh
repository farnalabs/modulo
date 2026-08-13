#!/usr/bin/env bash
# Unit tests for .github/scripts/pr-review-dedup.sh.
#
# Exercises the dedup decision logic against realistic GitHub API payload
# shapes. The multi-line-body cases are regression tests: the previous inline
# implementation joined commit_id|state|body into one string and split on '|',
# which corrupted the SHA/state whenever a review body spanned more than one
# line (the reviewer-side bodies are routinely 6-23 lines), silently defeating
# the dedup (fail-open) for essentially every real PR.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SCRIPT_DIR/pr-review-dedup.sh"

PASS=0
FAIL=0

# Build a GitHub review object.
review() {
  local commit_id="$1" state="$2" body="$3"
  jq -cn --arg id "$commit_id" --arg st "$state" --arg b "$body" \
    '{id: 1, state: $st, commit_id: $id, body: $b}'
}

# Build a GitHub commit object. parents=1 => normal commit, 2 => merge commit.
commit() {
  local sha="$1" parents="$2"
  if [ "$parents" = "2" ]; then
    jq -cn --arg sha "$sha" '{sha: $sha, parents: [{sha: "p0"}, {sha: "p1"}]}'
  else
    jq -cn --arg sha "$sha" '{sha: $sha, parents: [{sha: "p0"}]}'
  fi
}

# Run one case. Reads the current REVIEWS_JSON/COMMITS_JSON/HEAD_SHA globals.
run_case() {
  local name="$1" expected="$2"
  local output got
  output=$(REVIEWS_JSON="$REVIEWS_JSON" COMMITS_JSON="$COMMITS_JSON" HEAD_SHA="$HEAD_SHA" bash "$SCRIPT" 2>&1) || true
  got=$(printf '%s\n' "$output" | sed -n 's/^skip=//p' | head -1)
  if [ "$got" = "$expected" ]; then
    PASS=$((PASS + 1))
    echo "PASS: $name (skip=$got)"
  else
    FAIL=$((FAIL + 1))
    echo "FAIL: $name (expected skip=$expected)"
    echo "  output: $(printf '%s\n' "$output" | tr '\n' ' ')"
  fi
}

HEAD_SHA="sha-head"
COMMITS_JSON="[]"

# 1. No prior review -> dispatch.
REVIEWS_JSON="[]"
run_case "no prior review dispatches" false

# 2. Reviews API failure (caller passes "[]") -> dispatch (fail open).
REVIEWS_JSON="[]"
run_case "reviews API failure dispatches (fail open)" false

# 3. Only PENDING reviews -> dispatch.
REVIEWS_JSON="[$(review "sha-head" "PENDING" "pending body")]"
run_case "all-PENDING prior reviews dispatch" false

# 4. Same-SHA already reviewed -> skip.
REVIEWS_JSON="[$(review "sha-head" "APPROVED" "approved body")]"
run_case "same-SHA already reviewed skips" true

# 5. Fix push (non-merge commit since review) -> dispatch.
REVIEWS_JSON="[$(review "sha-old" "CHANGES_REQUESTED" "please fix")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-fix" 1), $(commit "sha-head" 1)]"
run_case "fix push dispatches" false

# 6. Merge-only head move under APPROVED -> skip.
REVIEWS_JSON="[$(review "sha-old" "APPROVED" "lgtm")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-merge" 2)]"
run_case "merge-only + APPROVED skips" true

# 7. Merge-only head move under merge-conflict CHANGES_REQUESTED -> dispatch.
REVIEWS_JSON="[$(review "sha-old" "CHANGES_REQUESTED" "Resolve the merge conflict before this ships")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-merge" 2)]"
run_case "merge-only + CR-with-merge-conflict dispatches" false

# 8. Merge-only head move under CHANGES_REQUESTED without conflict marker -> skip.
REVIEWS_JSON="[$(review "sha-old" "CHANGES_REQUESTED" "Please add tests for the new endpoint")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-merge" 2)]"
run_case "merge-only + CR-without-conflict skips" true

# 9. Merge-only head move under COMMENTED -> dispatch (non-decisive review).
REVIEWS_JSON="[$(review "sha-old" "COMMENTED" "just a note")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-merge" 2)]"
run_case "merge-only + COMMENTED dispatches" false

# 10. Merge-only head move under DISMISSED -> dispatch (non-decisive review).
REVIEWS_JSON="[$(review "sha-old" "DISMISSED" "")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-merge" 2)]"
run_case "merge-only + DISMISSED dispatches" false

# 11. Force-push rewrite: reviewed SHA absent from current history -> dispatch.
REVIEWS_JSON="[$(review "sha-gone" "APPROVED" "lgtm")]"
COMMITS_JSON="[$(commit "sha-new" 1)]"
run_case "force-push rewrite dispatches (fail open)" false

# 12. Commits API failure ("[]") -> reviewed SHA not found -> dispatch (fail open).
REVIEWS_JSON="[$(review "sha-old" "APPROVED" "lgtm")]"
COMMITS_JSON="[]"
run_case "commits API failure dispatches (fail open)" false

# 13. Regression: multi-line body (with pipes) must not corrupt same-SHA match.
MULTI_BODY=$(printf 'Looks good\nShip it | really\n\n3rd line')
REVIEWS_JSON="[$(review "sha-head" "APPROVED" "$MULTI_BODY")]"
COMMITS_JSON="[]"
run_case "multi-line body same-SHA skips (parsing regression)" true

# 14. Regression: multi-line body with conflict marker in merge-only path.
MULTI_BODY=$(printf 'First line\nThere is a merge conflict to resolve\nLast line')
REVIEWS_JSON="[$(review "sha-old" "CHANGES_REQUESTED" "$MULTI_BODY")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-merge" 2)]"
run_case "multi-line conflict body merge-only dispatches (parsing regression)" false

# 15. Regression: multi-line body without conflict marker still skips.
MULTI_BODY=$(printf 'Fix the tests\nThanks | really')
REVIEWS_JSON="[$(review "sha-old" "CHANGES_REQUESTED" "$MULTI_BODY")]"
COMMITS_JSON="[$(commit "sha-old" 1), $(commit "sha-merge" 2)]"
run_case "multi-line non-conflict body merge-only skips (parsing regression)" true

echo ""
echo "pr-review-dedup tests: ${PASS} passed, ${FAIL} failed"
if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
