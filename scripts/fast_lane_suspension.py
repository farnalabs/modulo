#!/usr/bin/env python3
"""Fast-lane suspension signal helpers (ADR 041 Decision 3.4).

Parses the ``<!-- FAST_LANE_SUSPENSION: <severity> -->`` marker from PR
review comments and computes the 24-hour suspension window.

The PR Reviewer posts this HTML comment as its **last line** when it
discovers a critical or major finding on a fast-lane-merged PR.  This
module is the pure-logic layer consumed by the
``fast-lane-suspension.yml`` workflow.

Usage (pure-logic helpers — no network calls)::

    from fast_lane_suspension import parse_suspension_marker, compute_expiry, build_reason

    severity = parse_suspension_marker(comment_body)  # "critical" | "major" | None
    expiry = compute_expiry()                          # ISO-8601 UTC string, now + 24h
    reason = build_reason(pr_number=712, severity="critical")

Exit codes:
    0  — success
    2  — usage / argument error
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Marker detection
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(
    r"^\s*<!--\s*FAST_LANE_SUSPENSION:\s*(critical|major)\s*-->\s*$",
    re.MULTILINE | re.IGNORECASE,
)

_VALID_SEVERITIES = frozenset({"critical", "major"})

# Fenced code block pattern: lines between ``` or ~~~ openers and closers.
# We strip these regions before scanning for the marker so a quoted example
# like
#     ```
#     <!-- FAST_LANE_SUSPENSION: critical -->
#     ```
# does not trigger a suspension.
_FENCED_BLOCK_RE = re.compile(
    r"^(?:```|~~~).*$(?P<body>.*?)(?:\n```|\n~~~)$",
    re.MULTILINE | re.DOTALL,
)


def parse_suspension_marker(comment_body: str) -> str | None:
    """Return the severity (``"critical"`` / ``"major"``) if *comment_body*
    carries a valid suspension marker, else ``None``.

    **Strict matching rules** — applied in order:

    1. Only the exact HTML-comment form ``<!-- FAST_LANE_SUSPENSION: <sev> -->``
       counts.  A severity word appearing in prose does not match.
    2. The match is case-insensitive on the severity value (``CRITICAL``,
       ``Major``) but must otherwise be the exact token — whitespace around
       the severity value is stripped and compared against the canonical
       lowercase forms.
    3. Markers inside fenced code blocks (```` ``` ```` or ``~~~``) are
       ignored: the entire fenced region is removed before scanning, so a
       quoted example of the format does not trigger a suspension.

    Returns:
        ``"critical"`` or ``"major"`` when matched, ``None`` otherwise.
    """
    if not comment_body:
        return None

    # Strip fenced code blocks so quoted examples are not matched.
    clean = _FENCED_BLOCK_RE.sub("", comment_body)

    m = _MARKER_RE.search(clean)
    if m is None:
        return None

    severity = m.group(1).lower()
    if severity not in _VALID_SEVERITIES:
        return None

    return severity


# ---------------------------------------------------------------------------
# Expiry computation
# ---------------------------------------------------------------------------

SUSPENSION_WINDOW_HOURS = 24


def compute_expiry(now: datetime | None = None) -> str:
    """Return the suspension expiry as an ISO-8601 UTC string.

    The expiry is *now* + 24 hours.  Accepts an optional *now* parameter
    for deterministic testing; when omitted the real UTC clock is used.

    Returns:
        ISO-8601 UTC string, e.g. ``"2026-09-22T14:30:00+00:00"``.
    """
    if now is None:
        now = datetime.now(tz=UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    expiry = now + timedelta(hours=SUSPENSION_WINDOW_HOURS)
    return expiry.isoformat()


# ---------------------------------------------------------------------------
# Reason string
# ---------------------------------------------------------------------------


def build_reason(pr_number: int, severity: str, now: datetime | None = None) -> str:
    """Build a human-readable suspension reason string.

    Args:
        pr_number: The merged PR number that triggered the suspension.
        severity: ``"critical"`` or ``"major"``.
        now: Optional reference time (for deterministic testing).

    Returns:
        A string like ``"PR #712: critical finding (2026-09-21)"``.
    """
    if now is None:
        now = datetime.now(tz=UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    date_str = now.strftime("%Y-%m-%d")
    return f"PR #{pr_number}: {severity} finding ({date_str})"


# ---------------------------------------------------------------------------
# CLI entry-point (standalone test harness — not used by the workflow)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Minimal CLI for ad-hoc testing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comment-body",
        required=True,
        help="Comment body to parse for the suspension marker",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        default=0,
        help="PR number for the reason string (optional)",
    )
    args = parser.parse_args(argv)

    severity = parse_suspension_marker(args.comment_body)
    if severity is None:
        print("No suspension marker found.")
        return 0

    expiry = compute_expiry()
    reason = build_reason(args.pr_number, severity) if args.pr_number else ""
    print(f"Severity:   {severity}")
    print(f"Expiry:     {expiry}")
    if reason:
        print(f"Reason:     {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
