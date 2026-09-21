#!/usr/bin/env python3
"""Unit tests for scripts/fast_lane_suspension.py.

Run from the repo root:
    uv run --project backend pytest tests/unit/scripts/test_fast_lane_suspension.py -q
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Ensure the scripts directory is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fast_lane_suspension import (  # noqa: E402
    SUSPENSION_WINDOW_HOURS,
    build_reason,
    compute_expiry,
    parse_suspension_marker,
)

# ---------------------------------------------------------------------------
# parse_suspension_marker
# ---------------------------------------------------------------------------


class TestParseSuspensionMarker:
    """Detect the <!-- FAST_LANE_SUSPENSION: <sev> --> marker in comment bodies."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            # Valid markers — exact form
            ("<!-- FAST_LANE_SUSPENSION: critical -->", "critical"),
            ("<!-- FAST_LANE_SUSPENSION: major -->", "major"),
            # Leading/trailing whitespace around the marker line is fine
            ("  <!-- FAST_LANE_SUSPENSION: critical -->  ", "critical"),
            ("<!-- FAST_LANE_SUSPENSION: major -->\n", "major"),
            # Mixed-case severity (case-insensitive on the value)
            ("<!-- FAST_LANE_SUSPENSION: CRITICAL -->", "critical"),
            ("<!-- FAST_LANE_SUSPENSION: Major -->", "major"),
            # Marker embedded in a longer comment (last line)
            (
                (
                    "The review found issues in the pipeline engine.\n"
                    "File: backend/src/modulo/core/pipeline_engine/graph.py\n"
                    "Severity: critical\n"
                    "<!-- FAST_LANE_SUSPENSION: critical -->"
                ),
                "critical",
            ),
            # Extra whitespace inside the comment delimiters
            ("<!--  FAST_LANE_SUSPENSION:  critical  -->", "critical"),
        ],
        ids=[
            "critical_exact",
            "major_exact",
            "leading_trailing_whitespace",
            "trailing_newline",
            "critical_uppercase",
            "major_mixed_case",
            "embedded_in_longer_comment",
            "extra_internal_whitespace",
        ],
    )
    def test_marker_present(self, body: str, expected: str) -> None:
        assert parse_suspension_marker(body) == expected

    @pytest.mark.parametrize(
        "body",
        [
            # Empty / None
            "",
            # No marker at all
            "The review looks good, no issues found.",
            # Severity word in prose (not the marker form)
            "This is a critical finding that needs attention.",
            "The major issue was found in the pipeline.",
            "Severity: critical — please review.",
            # Similar but wrong prefix
            "<!-- FAST_LANE_SUSPEND: critical -->",
            "<!-- FAST_LANE_SUSPENSION: minor -->",  # minor is not valid
            # Marker without HTML comment wrapping
            "FAST_LANE_SUSPENSION: critical",
            # Partial marker (missing closing -->)
            "<!-- FAST_LANE_SUSPENSION: critical --",
            # Marker with wrong separator
            "<!-- FAST_LANE_SUSPENSION = critical -->",
            # HTML comment that looks similar but different
            "<!-- SUSPENSION: critical -->",
        ],
        ids=[
            "empty_string",
            "no_marker",
            "severity_in_prose_critical",
            "severity_in_prose_major",
            "severity_in_prose_colon",
            "wrong_prefix",
            "invalid_severity_minor",
            "no_html_comment_wrapping",
            "unclosed_html_comment",
            "wrong_separator",
            "similar_but_different",
        ],
    )
    def test_marker_absent(self, body: str) -> None:
        assert parse_suspension_marker(body) is None

    def test_marker_inside_fenced_code_block_is_ignored(self) -> None:
        """A marker quoted inside a fenced code block must NOT trigger."""
        body = (
            "The reviewer posts a marker like this:\n"
            "\n"
            "```\n"
            "<!-- FAST_LANE_SUSPENSION: critical -->\n"
            "```\n"
            "\n"
            "But this comment itself is clean."
        )
        assert parse_suspension_marker(body) is None

    def test_marker_inside_tilde_fenced_block_is_ignored(self) -> None:
        """Tilde-fenced blocks (~~~) are also stripped."""
        body = "Example:\n\n~~~\n<!-- FAST_LANE_SUSPENSION: major -->\n~~~\n"
        assert parse_suspension_marker(body) is None

    def test_real_marker_outside_fence_is_detected(self) -> None:
        """A real marker outside a code block is still detected."""
        body = (
            "Here is the format:\n"
            "\n"
            "```\n"
            "<!-- FAST_LANE_SUSPENSION: critical -->\n"
            "```\n"
            "\n"
            "And here is the actual marker:\n"
            "<!-- FAST_LANE_SUSPENSION: major -->"
        )
        assert parse_suspension_marker(body) == "major"

    def test_empty_body(self) -> None:
        assert parse_suspension_marker("") is None


# ---------------------------------------------------------------------------
# compute_expiry
# ---------------------------------------------------------------------------


class TestComputeExpiry:
    """Expiry must be exactly 24 hours in the future, as ISO-8601 UTC."""

    def test_expiry_is_24h_ahead(self) -> None:
        now = datetime(2026, 9, 21, 10, 0, 0, tzinfo=UTC)
        expiry_str = compute_expiry(now=now)
        expiry_dt = datetime.fromisoformat(expiry_str)
        assert expiry_dt == now + timedelta(hours=24)

    def test_expiry_is_iso8601_utc(self) -> None:
        now = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
        expiry_str = compute_expiry(now=now)
        # Must contain +00:00 for UTC
        assert "+00:00" in expiry_str

    def test_expiry_without_now_uses_utc(self) -> None:
        """Without an explicit now, expiry should be ~24h from real time."""
        before = datetime.now(tz=UTC)
        expiry_str = compute_expiry()
        expiry_dt = datetime.fromisoformat(expiry_str)
        after = before + timedelta(hours=24)
        # Allow 1 second of clock drift
        assert expiry_dt >= before + timedelta(hours=24) - timedelta(seconds=1)
        assert expiry_dt <= after + timedelta(seconds=1)


# ---------------------------------------------------------------------------
# build_reason
# ---------------------------------------------------------------------------


class TestBuildReason:
    """The reason string must include the PR number and severity."""

    def test_contains_pr_number(self) -> None:
        reason = build_reason(pr_number=712, severity="critical")
        assert "712" in reason

    def test_contains_severity(self) -> None:
        reason = build_reason(pr_number=100, severity="major")
        assert "major" in reason

    def test_contains_date(self) -> None:
        now = datetime(2026, 9, 21, 14, 30, 0, tzinfo=UTC)
        reason = build_reason(pr_number=42, severity="critical", now=now)
        assert "2026-09-21" in reason

    def test_format(self) -> None:
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        reason = build_reason(pr_number=999, severity="critical", now=now)
        assert reason == "PR #999: critical finding (2026-01-01)"


# ---------------------------------------------------------------------------
# SUSPENSION_WINDOW_HOURS constant
# ---------------------------------------------------------------------------


class TestSuspensionWindow:
    def test_window_is_24_hours(self) -> None:
        assert SUSPENSION_WINDOW_HOURS == 24
