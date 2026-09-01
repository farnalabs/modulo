#!/usr/bin/env python3
"""Merge-queue SHA-pin TOCTOU guard (FAR-455).

Reads the GitHub API responses for a PR's reviews and commits (as written by
`gh api --paginate`, which emits ONE JSON document PER PAGE, concatenated with no
separator) plus the current head SHA, and prints exactly one verdict line to
stdout::

    ok|<reason>     -> the latest APPROVED review still covers the head; merge allowed
    stale|<reason>  -> the approval is stale (or unverifiable); DO NOT merge (fail closed)

The decision walks the FULL commit list and blocks when ANY non-merge (single
parent) commit was committed AFTER the latest APPROVED review's ``submitted_at``.
This is the timestamp-based staleness check documented in merge-queue.yml: it
cannot be bypassed by pushing a post-approval fix commit and then hiding it
behind a later merge (HEAD becomes 2-parent), and it correctly keeps the
approval valid across merge-only head moves (every commit >= 2 parents).

Crucially it merges the ``gh api --paginate`` pages into a single list FIRST, so
the >100-item (multi-page) large-PR path -- which the prior bash implementation
failed OPEN on (jq saw only the first page, the count became multi-line, and the
``-gt 0`` test silently evaluated false) -- is handled correctly. Any
parse/IO error returns ``stale`` (fail closed), never ``ok``.

This intentionally mirrors the proven walk in ``.github/scripts/pr-review-dedup.py``
so the logic lives in versioned, unit-tested Python rather than bash.

Unit tests: backend/tests/unit/scripts/test_merge_queue_approve_guard.py.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime


def _parse_iso(ts: str) -> int:
    """Parse a GitHub ISO-8601 timestamp (UTC, 'Z' suffix) to a unix epoch."""
    return int(datetime.fromisoformat(ts).timestamp())


def load_concatenated(path: str):
    """Load a file that may contain multiple concatenated JSON documents.

    ``gh api --paginate`` writes each page as a separate top-level JSON array,
    concatenated directly (e.g. ``[page1][page2]``). A naive ``json.load`` only
    sees the first page -- exactly the multi-page bug this script must avoid.
    We walk the text with ``raw_decode`` and flatten every top-level array/dict
    into a single list.
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    decoder = json.JSONDecoder()
    items: list = []
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(text, idx)
        if isinstance(obj, list):
            items.extend(obj)
        elif isinstance(obj, dict):
            items.append(obj)
        idx = end
    return items


def decide(reviews, commits, head_sha: str | None = None):
    """Return (verdict, reason). verdict is ``'ok'`` or ``'stale'``."""
    approved = [r for r in reviews if r.get("state") == "APPROVED"]
    if not approved:
        return "stale", "no APPROVED review found - treat as not-approved (fail closed)"
    last = approved[-1]  # GitHub returns reviews oldest-first
    submitted = last.get("submitted_at")
    if not submitted:
        return "stale", "could not resolve approval timestamp - fail closed"
    try:
        approve_epoch = _parse_iso(submitted)
    except Exception:
        return "stale", f"approval timestamp parse failed ({submitted!r}) - fail closed"

    stale = 0
    for c in commits:
        parents = c.get("parents") or []
        if len(parents) != 1:
            continue  # merge commits keep the approval valid
        cd = (c.get("commit") or {}).get("committer", {}).get("date")
        if not cd:
            # Missing committer date -> unverifiable -> fail closed (stale),
            # matching the reviews side and the documented contract.
            stale += 1
            continue
        try:
            ce = _parse_iso(cd)
        except Exception:
            # Malformed committer date -> unverifiable -> fail closed (stale),
            # matching the reviews side and the documented contract.
            stale += 1
            continue
        if ce > approve_epoch:
            stale += 1

    if stale > 0:
        return (
            "stale",
            f"{stale} non-merge commit(s) committed after approval ({submitted}) - approval stale, TOCTOU guard",
        )
    approve_commit = last.get("commit_id") or "?"
    head = head_sha or "?"
    return "ok", f"SHA-pin guard OK (approve commit_id={approve_commit}, head={head})"


def main(argv):
    if len(argv) < 3:
        print(
            "stale|usage: merge-queue-approve-guard.py <reviews.json> <commits.json> [head_sha]",
            flush=True,
        )
        return 2
    try:
        reviews = load_concatenated(argv[1])
        commits = load_concatenated(argv[2])
    except Exception as exc:  # fail closed on any I/O or JSON error
        print(f"stale|error loading inputs: {exc} - fail closed", flush=True)
        return 1
    head_sha = argv[3] if len(argv) > 3 else None
    verdict, reason = decide(reviews, commits, head_sha)
    print(f"{verdict}|{reason}", flush=True)
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
