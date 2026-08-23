#!/usr/bin/env python3
"""Decide whether the PR Reviewer webhook should fire for a given head.

The ci.yml notify-pr-review job invokes this script with three arguments:

    python3 pr-review-dedup.py <reviews.json> <commits.json> <HEAD_SHA>

* <reviews.json> - JSON array of review objects as returned by the GitHub
  `pulls/{n}/reviews` API (each has commit_id, state, body, submitted_at).
* <commits.json> - JSON array of commit objects as returned by the `pulls/{n}/commits`
  API (each has sha and parents), oldest first, HEAD being the last entry.
* <HEAD_SHA> - the current head SHA of the pull request.

It prints exactly one line to stdout: either `skip=true` (the PR Reviewer
should NOT be re-invoked) or `skip=false` (it should). The decision must be
robust to multi-line review bodies and pipe characters in body text, which
break naive line-oriented shell parsing. It is fail-open (dispatch) on any
load error or ambiguous state: in the worst case an extra review runs, but a
missed review can deadlock a PR in CHANGES_REQUESTED indefinitely.
"""

import json
import sys


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def latest_review(reviews):
    """Return the most recent review, oldest -> newest, by submitted_at."""
    chronological = sorted(reviews, key=lambda r: r.get("submitted_at") or "")
    return chronological[-1]


def skip_decision(reviews, commits, head_sha):
    """Return 'true'/'false' as a string for whether the review should skip."""
    if not reviews:
        # No prior review: always dispatch.
        return "false"

    review = latest_review(reviews)
    review_sha = review.get("commit_id") or ""
    state = (review.get("state") or "").upper()

    if review_sha == head_sha:
        # HEAD has not moved since the last review: nothing new to review.
        return "true"

    # HEAD moved. Determine whether the movement is merge-only or includes
    # a real (non-merge) commit after the reviewed SHA.
    commit_shas = [c.get("sha") for c in commits]
    if review_sha not in commit_shas:
        # The reviewed SHA no longer exists in the commit history (a
        # force-push rewrote it): cannot prove nothing changed, fail open.
        return "false"

    review_index = commit_shas.index(review_sha)
    moved = commits[review_index + 1:]

    if any(len(c.get("parents", [])) < 2 for c in moved):
        # A non-merge commit landed after the last review: real new code.
        return "false"

    # Merge-only movement. If the prior state was APPROVED, the approval
    # stands (no new code, merge realignment only). Any CHANGES_REQUESTED /
    # COMMENTED / DISMISSED state must fail open: the merge may have resolved
    # the CI failure or conflict, and only a re-review can unblock the PR.
    if state == "APPROVED":
        return "true"
    return "false"


def main(argv):
    try:
        reviews = load_json(argv[1])
        commits = load_json(argv[2])
        head_sha = argv[3]
        decision = skip_decision(reviews, commits, head_sha)
    except Exception as exc:  # noqa: BLE001 - fail open on any load/parse error
        sys.stderr.write("pr-review-dedup: fail-open (%s); dispatching review\n" % exc)
        decision = "false"
    print("skip=%s" % decision)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))