#!/usr/bin/env python3
"""Unit tests for scripts/fast_lane_audit.py.

Run from the repo root:
    uv run --project backend pytest tests/unit/scripts/test_fast_lane_audit.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Ensure the scripts directory is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fast_lane_audit import (  # noqa: E402
    Comment,
    Merge,
    find_unaudited_merges,
)
from fast_lane_audit import main as audit_main  # noqa: E402

# ---------------------------------------------------------------------------
# find_unaudited_merges — pure decision logic
# ---------------------------------------------------------------------------

_MERGE_TIME = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


class TestFindUnauditedMerges:
    """Core decision logic: which merges lack a qualifying review."""

    def test_audited_merge(self) -> None:
        """A merge with a post-merge comment by the reviewer is audited."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [Comment("modulo-reviewbot", _MERGE_TIME + timedelta(hours=1), 100)]
        unaudited = find_unaudited_merges(merges, comments)
        assert unaudited == []

    def test_unaudited_merge(self) -> None:
        """A merge with no reviewer comments is unaudited."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments: list[Comment] = []
        unaudited = find_unaudited_merges(merges, comments)
        assert len(unaudited) == 1
        assert unaudited[0].pr_number == 100

    def test_comment_predates_merge(self) -> None:
        """A comment before the merge is the pre-merge review and does NOT count."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [
            # Before the merge — this is the pre-merge review
            Comment("modulo-reviewbot", _MERGE_TIME - timedelta(hours=2), 100)
        ]
        unaudited = find_unaudited_merges(merges, comments)
        assert len(unaudited) == 1, "Pre-merge comment must not count as audit"

    def test_comment_outside_window(self) -> None:
        """A comment after the merge but beyond the window does NOT count."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [
            # 25 hours after merge — outside the default 24h window
            Comment("modulo-reviewbot", _MERGE_TIME + timedelta(hours=25), 100)
        ]
        unaudited = find_unaudited_merges(merges, comments)
        assert len(unaudited) == 1, "Comment outside window must not count"

    def test_comment_at_window_boundary(self) -> None:
        """A comment exactly at the window boundary counts (inclusive)."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [
            # Exactly 24 hours after merge — at the boundary
            Comment("modulo-reviewbot", _MERGE_TIME + timedelta(hours=24), 100)
        ]
        unaudited = find_unaudited_merges(merges, comments)
        assert unaudited == [], "Comment at window boundary must count"

    def test_no_comments_at_all(self) -> None:
        """A merge with zero comments on its PR is unaudited."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        # Comments on a different PR
        comments = [Comment("modulo-reviewbot", _MERGE_TIME + timedelta(hours=1), 200)]
        unaudited = find_unaudited_merges(merges, comments)
        assert len(unaudited) == 1

    def test_wrong_reviewer(self) -> None:
        """A comment by someone other than the reviewer does NOT count."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [Comment("some-other-user", _MERGE_TIME + timedelta(hours=1), 100)]
        unaudited = find_unaudited_merges(merges, comments)
        assert len(unaudited) == 1

    def test_custom_reviewer(self) -> None:
        """The reviewer identity is configurable."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [Comment("custom-reviewer", _MERGE_TIME + timedelta(hours=1), 100)]
        unaudited = find_unaudited_merges(merges, comments, reviewer="custom-reviewer")
        assert unaudited == []

    def test_zero_window_no_upper_bound(self) -> None:
        """window_hours=0 means no upper bound — any post-merge comment counts."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [
            # 100 hours after merge — with window_hours=0, this still counts
            Comment("modulo-reviewbot", _MERGE_TIME + timedelta(hours=100), 100)
        ]
        unaudited = find_unaudited_merges(merges, comments, window_hours=0)
        assert unaudited == []

    def test_multiple_merges_partial_audit(self) -> None:
        """Some merges audited, some not — only unaudited are returned."""
        merges = [
            Merge("aaa111", 100, _MERGE_TIME),
            Merge("bbb222", 101, _MERGE_TIME),
            Merge("ccc333", 102, _MERGE_TIME),
        ]
        comments = [
            # PR 100: audited
            Comment("modulo-reviewbot", _MERGE_TIME + timedelta(hours=1), 100),
            # PR 102: audited
            Comment("modulo-reviewbot", _MERGE_TIME + timedelta(hours=2), 102),
            # PR 101: no comment — unaudited
        ]
        unaudited = find_unaudited_merges(merges, comments)
        assert len(unaudited) == 1
        assert unaudited[0].pr_number == 101

    def test_empty_merges(self) -> None:
        """No merges to audit returns empty list."""
        unaudited = find_unaudited_merges([], [])
        assert unaudited == []

    def test_comment_at_exact_merge_time_does_not_count(self) -> None:
        """Only comments *strictly after* the merge count — same-instant
        comments are the pre-merge review and are not an audit."""
        merges = [Merge("abc123", 100, _MERGE_TIME)]
        comments = [Comment("modulo-reviewbot", _MERGE_TIME, 100)]
        unaudited = find_unaudited_merges(merges, comments)
        assert len(unaudited) == 1, "A comment at the exact merge instant must not count"


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------


class TestMain:
    """CLI contract: exit codes and JSON parsing."""

    def test_all_audited_exits_zero(self) -> None:
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00Z"}]
        comments = [{"author": "modulo-reviewbot", "created_at": "2026-09-22T13:00:00Z", "pr_number": 1}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                json.dumps(comments),
            ]
        )
        assert rc == 0

    def test_unaudited_exits_one(self) -> None:
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00Z"}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                "[]",
            ]
        )
        assert rc == 1

    def test_bad_merges_json_exits_two(self) -> None:
        rc = audit_main(
            [
                "--merges-json",
                "not-json",
                "--comments-json",
                "[]",
            ]
        )
        assert rc == 2

    def test_bad_comments_json_exits_two(self) -> None:
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00Z"}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                "not-json",
            ]
        )
        assert rc == 2

    def test_missing_field_exits_two(self) -> None:
        merges = [{"commit": "abc"}]  # missing pr_number and merged_at
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                "[]",
            ]
        )
        assert rc == 2


class TestMainNaiveTimestamps:
    """tz-naive JSON timestamps must be treated as UTC (``_parse_dt``)."""

    def test_naive_merge_with_aware_comment_counts(self) -> None:
        """A naive merge timestamp + an aware (Z) comment never raises
        TypeError and is audited — proving naive values are normalised
        to UTC before comparison."""
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00"}]
        comments = [{"author": "modulo-reviewbot", "created_at": "2026-09-22T13:00:00Z", "pr_number": 1}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                json.dumps(comments),
            ]
        )
        assert rc == 0, "a naive merge time (UTC) must compare cleanly with an aware comment"

    def test_aware_merge_with_naive_comment_counts(self) -> None:
        """A naive comment timestamp is normalised to UTC as well."""
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00Z"}]
        comments = [{"author": "modulo-reviewbot", "created_at": "2026-09-22T13:00:00", "pr_number": 1}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                json.dumps(comments),
            ]
        )
        assert rc == 0

    def test_two_naive_timestamps_count(self) -> None:
        """Both timestamps naive and within the window: audited."""
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00"}]
        comments = [{"author": "modulo-reviewbot", "created_at": "2026-09-22T13:00:00", "pr_number": 1}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                json.dumps(comments),
            ]
        )
        assert rc == 0


class TestMainWindowHoursFlag:
    """The ``--window-hours`` CLI flag must reach the decision logic."""

    def test_out_of_window_comment_with_default_window(self) -> None:
        """A +30h comment is outside the default 24h window → unaudited."""
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00Z"}]
        comments = [{"author": "modulo-reviewbot", "created_at": "2026-09-23T18:00:00Z", "pr_number": 1}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                json.dumps(comments),
            ]
        )
        assert rc == 1

    def test_out_of_window_comment_fits_extended_window(self) -> None:
        """The same +30h comment is audited once the window is widened."""
        merges = [{"commit": "abc", "pr_number": 1, "merged_at": "2026-09-22T12:00:00Z"}]
        comments = [{"author": "modulo-reviewbot", "created_at": "2026-09-23T18:00:00Z", "pr_number": 1}]
        rc = audit_main(
            [
                "--merges-json",
                json.dumps(merges),
                "--comments-json",
                json.dumps(comments),
                "--window-hours",
                "48",
            ]
        )
        assert rc == 0, "--window-hours must widen the reviewed window"
