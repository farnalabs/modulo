#!/usr/bin/env python3
"""PR Reviewer trigger-side dedup decision logic (ci.yml notify-pr-review).

Reads two JSON files (the GitHub API responses for a PR's reviews and
commits) and a head SHA, then prints exactly one line: `skip=true` or
`skip=false`.

The ci.yml notify-pr-review job calls this instead of inline bash because
the decision needs to handle multi-line review bodies and pipe characters
in body text, which naive `cut -d'|'` parsing mangles (cut is
line-oriented, so continuation lines bleed into the parsed SHA). Keeping
the logic in a versioned script also makes it unit-testable (see
backend/tests/unit/scripts/test_pr_review_dedup.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

Review = dict[str, Any]
Commit = dict[str, Any]


def last_review(reviews: list[Review]) -> Review | None:
    """Return the most recent non-PENDING review dict, or None."""
    non_pending = [r for r in reviews if r.get("state") != "PENDING"]
    return non_pending[-1] if non_pending else None


def non_merge_commits_since(commits: list[Commit], last_sha: str) -> tuple[bool, int]:
    """Return (found_last, count_non_merge_since).

    Walk the PR commit list newest-first (reverse order) until we hit the
    commit the last review was posted against. Count commits with exactly one
    parent (non-merge). A force-push may have rewritten history so the last
    review SHA no longer exists -> found_last=False.
    """
    count = 0
    found = False
    for c in reversed(commits):
        if c.get("sha") == last_sha:
            found = True
            break
        if len(c.get("parents", []) or []) == 1:
            count += 1
    return found, count


def decide(reviews: list[Review], commits: list[Commit], head_sha: str) -> tuple[bool, str]:
    """Return (skip: bool, reason: str).

    skip=True  -> the PR Reviewer webhook should NOT fire for this head.
    skip=False -> dispatch (first review, real fix, or unverifiable state).
    """
    rev = last_review(reviews)
    if rev is None:
        return False, "No prior review - dispatch first review."
    last_sha = rev.get("commit_id") or ""

    if last_sha == head_sha:
        return True, "Head already reviewed (same SHA) - skip."

    found, non_merge = non_merge_commits_since(commits, last_sha)
    if not found:
        # Force-push rewrote history (or API inconsistency). Fail open: we
        # cannot prove nothing changed, so dispatch.
        return (
            False,
            "Last review SHA not found in history (force-push?) - dispatch (fail open).",
        )
    if non_merge > 0:
        return False, f"{non_merge} non-merge commit(s) since last review - dispatch."

    # Zero non-merge commits: head moved only via merge commits (Branch Fixer
    # "merge origin/main" churn). No new PR code exists. Decide whether a
    # re-review can still change the outcome:
    state = rev.get("state") or ""
    if state == "APPROVED":
        # Approval stands: head moved only via merges, no new code. Skip.
        return True, "Only merge commits since last review (no new code) - skip."
    if state == "CHANGES_REQUESTED":
        # FAIL OPEN: a merge can resolve a CI-failure CR or realign a stale
        # base. A re-review can only re-confirm the CR (still blocked) or
        # approve (unblock) - never wrongly unblock. Skipping deadlocks the PR.
        return (
            False,
            "Prior review was a CHANGES_REQUESTED - merge may resolve it - dispatch (fail open).",
        )
    # COMMENTED / DISMISSED / empty: no decisive prior decision, so a fresh
    # formal review after a main merge is worth the cost (fail open).
    return False, f"Prior state {state or 'empty'} is not decisive - dispatch."


def load(path: str) -> Any:
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: pr-review-dedup.py <reviews.json> <commits.json> <head_sha>",
            file=sys.stderr,
        )
        return 2
    try:
        reviews = load(argv[1])
        commits = load(argv[2])
        head_sha = argv[3]
    except Exception as exc:  # fail open on any I/O or JSON error
        print("skip=false", flush=True)
        print(f"error loading inputs: {exc} - dispatch (fail open)", file=sys.stderr)
        return 0
    skip, reason = decide(reviews, commits, head_sha)
    print("skip=true" if skip else "skip=false", flush=True)
    print(reason, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
