#!/usr/bin/env python3
"""Audit fast-lane merges for a missing post-merge review.

Pure, testable decision logic — no GitHub API calls, no subprocess.
Given a list of fast-lane merges and a list of candidate review comments,
determine which merges are *unaudited*: a merge is audited when a comment
by the reviewer identity (``modulo-reviewbot``) exists on that PR
**created after the merge time** — comments before the merge are the
pre-merge review and do NOT count.

Usage (CI):
    python scripts/fast_lane_audit.py \\
        --window-hours 24 \\
        --reviewer modulo-reviewbot

The workflow (``fast-lane-post-merge-audit.yml``) feeds this with data
fetched from the GitHub API.  This script never calls GitHub itself.

Exit codes:
    0  — all merges audited (or no merges to audit)
    1  — unaudited merges found (the CI step should fail)
    2  — usage / argument error
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Merge:
    """A fast-lane merge to audit."""

    commit: str
    pr_number: int
    merged_at: datetime


@dataclass(frozen=True)
class Comment:
    """A candidate review comment on a PR."""

    author: str
    created_at: datetime
    pr_number: int


# ---------------------------------------------------------------------------
# Core decision logic
# ---------------------------------------------------------------------------


def find_unaudited_merges(
    merges: Sequence[Merge],
    comments: Sequence[Comment],
    *,
    reviewer: str = "modulo-reviewbot",
    window_hours: int = 24,
) -> list[Merge]:
    """Return the subset of *merges* that lack a qualifying post-merge review.

    A merge is considered **audited** when at least one comment by
    *reviewer* on the same PR was created **after** the merge time and
    **within** *window_hours* of the merge.  Comments before the merge
    are the pre-merge review and do NOT count.

    If *window_hours* is 0, no time upper-bound is applied — any
    post-merge comment by the reviewer suffices regardless of age.
    """
    unaudited: list[Merge] = []
    for merge in merges:
        # Collect comments on this PR that are strictly after the merge.
        post_merge = [
            c
            for c in comments
            if c.pr_number == merge.pr_number and c.author == reviewer and c.created_at > merge.merged_at
        ]
        if not post_merge:
            unaudited.append(merge)
            continue
        # Check the window: at least one comment must be within window_hours.
        if window_hours > 0:
            cutoff = merge.merged_at + timedelta(hours=window_hours)
            in_window = [c for c in post_merge if c.created_at <= cutoff]
            if not in_window:
                unaudited.append(merge)
    return unaudited


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating tz-naive as UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit fast-lane merges for a missing post-merge review.",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=24,
        help="Hours after merge within which a review comment counts (default: 24; 0 = no limit)",
    )
    parser.add_argument(
        "--reviewer",
        default="modulo-reviewbot",
        help="GitHub username of the reviewer identity (default: modulo-reviewbot)",
    )
    parser.add_argument(
        "--merges-json",
        required=True,
        help="JSON array of merges: [{commit, pr_number, merged_at}, ...]",
    )
    parser.add_argument(
        "--comments-json",
        required=True,
        help="JSON array of comments: [{author, created_at, pr_number}, ...]",
    )
    args = parser.parse_args(argv)

    try:
        raw_merges = json.loads(args.merges_json)
    except json.JSONDecodeError as exc:
        print(f"::error::Invalid merges JSON: {exc}", file=sys.stderr)
        return 2

    try:
        raw_comments = json.loads(args.comments_json)
    except json.JSONDecodeError as exc:
        print(f"::error::Invalid comments JSON: {exc}", file=sys.stderr)
        return 2

    merges: list[Merge] = []
    for m in raw_merges:
        try:
            merges.append(
                Merge(
                    commit=m["commit"],
                    pr_number=int(m["pr_number"]),
                    merged_at=_parse_dt(m["merged_at"]),
                )
            )
        except (KeyError, ValueError) as exc:
            print(f"::error::Bad merge entry {m}: {exc}", file=sys.stderr)
            return 2

    comments: list[Comment] = []
    for c in raw_comments:
        try:
            comments.append(
                Comment(
                    author=c["author"],
                    created_at=_parse_dt(c["created_at"]),
                    pr_number=int(c["pr_number"]),
                )
            )
        except (KeyError, ValueError) as exc:
            print(f"::error::Bad comment entry {c}: {exc}", file=sys.stderr)
            return 2

    unaudited = find_unaudited_merges(
        merges,
        comments,
        reviewer=args.reviewer,
        window_hours=args.window_hours,
    )

    if not unaudited:
        print(f"All {len(merges)} fast-lane merge(s) have been reviewed.")
        return 0

    for m in unaudited:
        print(
            f"::error::Unaudited fast-lane merge: PR #{m.pr_number} "
            f"(commit {m.commit}, merged {m.merged_at.isoformat()})"
        )
    print(f"{len(unaudited)} of {len(merges)} fast-lane merge(s) lack a post-merge review by {args.reviewer}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
