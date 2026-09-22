#!/usr/bin/env python3
"""Unit tests for scripts/fast_lane_classify.py.

Run from the repo root:
    uv run --project backend pytest tests/unit/scripts/test_fast_lane_classify.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the scripts directory is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fast_lane_classify import (  # noqa: E402
    _safe_arg,
    _safe_pr,
    _safe_repo,
    check_cap,
    check_label_live,
    check_no_test_weakening,
    check_sha_pinning,
    check_suspension,
    classify_path,
    classify_paths,
)
from fast_lane_classify import main as classify_main  # noqa: E402

# ---------------------------------------------------------------------------
# classify_path
# ---------------------------------------------------------------------------


class TestClassifyPath:
    """Each path must be class-A or class-B per the spec."""

    # --- Class-A paths ---
    @pytest.mark.parametrize(
        "path",
        [
            "backend/tests/unit/test_foo.py",
            "backend/tests/integration/test_bar.py",
            "frontend/tests/unit/baz.spec.ts",
            "tests/architecture/test_suite_quality.py",
            "docs/architecture.md",
            "README.md",
            ".github/CODEOWNERS",
            ".github/ISSUE_TEMPLATE/bug.md",
            ".github/pull_request_template.md",
        ],
    )
    def test_class_a_paths(self, path: str) -> None:
        assert classify_path(path) == "A", f"{path} should be class-A"

    # --- Class-B paths ---
    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            ".github/workflows/deploy.yaml",
            ".github/scripts/release-freeze-check.py",
            "scripts/fast_lane_classify.py",
            "pyproject.toml",
            "backend/pyproject.toml",
            "frontend/package.json",
            ".semgrep/rls_set_local.yml",
            ".pre-commit-config.yaml",
            "backend/src/modulo/api/routes/pipelines.py",
            "frontend/src/views/PipelinesView.vue",
            "fly.toml",
            "deploy/fly/entrypoint.sh",
            "backend/src/modulo/db/migrations/versions/0001_initial.py",
            "backend/src/modulo/core/pipeline_engine/graph.py",
        ],
    )
    def test_class_b_paths(self, path: str) -> None:
        assert classify_path(path) == "B", f"{path} should be class-B"


# ---------------------------------------------------------------------------
# classify_paths
# ---------------------------------------------------------------------------


class TestClassifyPaths:
    """A PR is class-A only if ALL its paths are class-A."""

    def test_all_a(self) -> None:
        paths = [
            "backend/tests/unit/test_foo.py",
            "docs/architecture.md",
        ]
        assert classify_paths(paths) == "A"

    def test_one_b_makes_all_b(self) -> None:
        paths = [
            "backend/tests/unit/test_foo.py",
            "backend/src/modulo/api/routes/pipelines.py",  # class-B
        ]
        assert classify_paths(paths) == "B"

    def test_empty_is_b(self) -> None:
        # Empty PR has no test changes — treat as class-B (not fast-lane eligible).
        assert classify_paths([]) == "B"

    def test_github_non_workflow_is_a(self) -> None:
        paths = [".github/CODEOWNERS"]
        assert classify_paths(paths) == "A"

    def test_github_workflow_is_b(self) -> None:
        paths = [".github/workflows/ci.yml"]
        assert classify_paths(paths) == "B"

    def test_yaml_in_github_is_b(self) -> None:
        # .yml/.yaml anywhere is class-B, even under .github/ non-workflow.
        paths = [".github/dependabot.yml"]
        assert classify_paths(paths) == "B"

    def test_md_files_are_a(self) -> None:
        paths = ["docs/security.md", "CHANGELOG.md", "frontend/README.md"]
        assert classify_paths(paths) == "A"

    def test_scripts_dir_is_b(self) -> None:
        paths = ["scripts/run_vulture.py"]
        assert classify_paths(paths) == "B"

    def test_deploy_dir_is_b(self) -> None:
        paths = ["deploy/fly/entrypoint.sh"]
        assert classify_paths(paths) == "B"

    def test_migrations_are_b(self) -> None:
        paths = ["backend/src/modulo/db/migrations/versions/0123_foo.py"]
        assert classify_paths(paths) == "B"

    def test_mixed_a_and_b(self) -> None:
        paths = [
            "backend/tests/unit/test_bar.py",
            "scripts/fast_lane_classify.py",  # class-B
            "docs/readme.md",
        ]
        assert classify_paths(paths) == "B"


# ---------------------------------------------------------------------------
# S8705 taint barriers: operator-supplied argv values are regex-bounded
# ---------------------------------------------------------------------------


class TestSafeArgGuards:
    """Bound --repo / --pr-number / diff-range before they reach subprocess."""

    @pytest.mark.parametrize(
        "repo",
        ["farnalabs/modulo", "farnalabs/modulo-new", "a/b"],
    )
    def test_safe_repo_accepts_valid(self, repo: str) -> None:
        assert _safe_repo(repo) == repo

    @pytest.mark.parametrize(
        "repo",
        [
            "",
            "no-slash",
            "--upload-pack=evil",
            "owner/repo; rm -rf /",
            "owner/repo --body-file /etc/passwd",
        ],
    )
    def test_safe_repo_rejects_injection(self, repo: str) -> None:
        assert _safe_repo(repo) is None

    @pytest.mark.parametrize("pr", [42, "42", "0"])
    def test_safe_pr_accepts_digits(self, pr: int | str) -> None:
        assert _safe_pr(pr) == str(pr)

    @pytest.mark.parametrize("pr", ["-1", "abc", "42; rm -rf /", ""])
    def test_safe_pr_rejects_non_digits(self, pr: str) -> None:
        assert _safe_pr(pr) is None

    def test_safe_arg_rejects_leading_dash(self) -> None:
        # A value that could be parsed as a CLI flag must never pass through.
        pattern = re.compile(r"[A-Za-z0-9./-]+")
        assert _safe_arg("-r/--upload-pack=evil", pattern) is None
        assert _safe_arg("origin/main", pattern) == "origin/main"


class TestNoTestWeakeningFailClosed:
    """An invalid diff range must fail closed — deny eligibility."""

    @patch("fast_lane_classify.subprocess.run")
    def test_invalid_ref_fails_closed(self, mock_run: MagicMock) -> None:
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main; rm -rf /", "HEAD")
        assert eligible is False
        assert len(violations) == 1
        assert "fail-closed" in violations[0]
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Exit-code contract: class-B exits non-zero, class-A exits 0
# ---------------------------------------------------------------------------


class TestMainExitCode:
    """Verify the exit-code contract that the CI step captures.

    The classifier step captures its exit code into a step output and always
    exits 0 (so the workflow stays "success" for every PR).  A separate step
    creates a named ``fast-lane-eligible`` check run: conclusion ``success``
    means class-A eligible, ``neutral`` means merely ineligible (class-B or
    unlabelled — not a failure), and ``failure`` is reserved for a genuine
    classifier error (rc=2).  The merge-queue reads that check run and grants
    the fast lane only on ``success``.  The script's non-zero exit for class-B
    is the signal the CI step captures — this test verifies that signal.
    """

    @patch("fast_lane_classify.subprocess.run")
    def test_class_b_exits_nonzero(self, mock_run: MagicMock) -> None:
        """A class-B PR (backend/src/ change) must exit 1."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="backend/src/modulo/api/routes/pipelines.py\n",
            stderr="",
        )
        rc = classify_main(
            [
                "--pr-number",
                "42",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
            ]
        )
        assert rc == 1, "class-B must exit 1 (check run = neutral, not a failure)"

    @patch("fast_lane_classify.check_sha_pinning", return_value=(True, "SHA-pinned"))
    @patch("fast_lane_classify.check_no_test_weakening", return_value=(True, []))
    @patch("fast_lane_classify.check_suspension", return_value=(True, "no suspension"))
    @patch("fast_lane_classify.check_cap", return_value=(True, "cap OK"))
    @patch("fast_lane_classify.check_label_live", return_value=(True, "label present"))
    @patch("fast_lane_classify.subprocess.run")
    def test_class_a_with_label_exits_zero(
        self,
        mock_run: MagicMock,
        mock_label: MagicMock,
        mock_cap: MagicMock,
        mock_susp: MagicMock,
        mock_weaken: MagicMock,
        mock_pin: MagicMock,
    ) -> None:
        """A class-A PR with the label and passing guardrails must exit 0."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="backend/tests/unit/test_foo.py\n",
            stderr="",
        )
        rc = classify_main(
            [
                "--pr-number",
                "42",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
            ]
        )
        assert rc == 0, "class-A with label and guardrails pass must exit 0"

    @patch("fast_lane_classify.check_label_live", return_value=(False, "label absent"))
    @patch("fast_lane_classify.subprocess.run")
    def test_class_a_without_label_exits_nonzero(
        self,
        mock_run: MagicMock,
        mock_label: MagicMock,
    ) -> None:
        """A class-A PR without the label must exit 1 (standard lane)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="backend/tests/unit/test_foo.py\n",
            stderr="",
        )
        rc = classify_main(
            [
                "--pr-number",
                "42",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
            ]
        )
        assert rc == 1, "class-A without label must exit 1 (standard lane)"


# ---------------------------------------------------------------------------
# Fail-closed guardrails: unresolvable checks deny eligibility (rc=1)
# ---------------------------------------------------------------------------


class TestCheckCapFailClosed:
    """check_cap must deny eligibility when it cannot resolve."""

    def test_invalid_repo_denies(self) -> None:
        eligible, detail = check_cap("invalid repo!; rm -rf /", 5, 24)
        assert eligible is False
        assert "fail-closed" in detail
        assert "invalid repo" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_api_error_denies(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="not found", stdout="")
        eligible, detail = check_cap("farnalabs/modulo", 5, 24)
        assert eligible is False
        assert "fail-closed" in detail
        assert "API error" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_timeout_denies(self, mock_run: MagicMock) -> None:
        import subprocess as _sp

        mock_run.side_effect = _sp.TimeoutExpired(cmd="gh", timeout=30)
        eligible, detail = check_cap("farnalabs/modulo", 5, 24)
        assert eligible is False
        assert "fail-closed" in detail


class TestCheckShaPinningFailClosed:
    """check_sha_pinning must deny eligibility when it cannot resolve."""

    def test_invalid_repo_denies(self) -> None:
        eligible, detail = check_sha_pinning("invalid repo!", 42, "abc123def456")
        assert eligible is False
        assert "fail-closed" in detail
        assert "invalid repo" in detail

    def test_invalid_pr_denies(self) -> None:
        eligible, detail = check_sha_pinning("farnalabs/modulo", -1, "abc123def456")
        assert eligible is False
        assert "fail-closed" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_subprocess_failure_denies(self, mock_run: MagicMock) -> None:
        import subprocess as _sp

        mock_run.side_effect = _sp.TimeoutExpired(cmd="gh", timeout=15)
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, "abc123def456")
        assert eligible is False
        assert "fail-closed" in detail
        assert "could not fetch PR head" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_empty_sha_denies(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, "abc123def456")
        assert eligible is False
        assert "fail-closed" in detail
        assert "empty head SHA" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_truncated_expected_sha_shows_length(self, mock_run: MagicMock) -> None:
        """When the caller passes a short SHA prefix, the mismatch message
        must label both values so they are distinguishable (FAR-1136
        deliverable 2).  A 12-char prefix never equals a 40-char head, so
        this is always a mismatch — but the message must be readable."""
        full_head = "72413204ff0dabcdef0123456789abcdef012345"
        truncated = "72413204ff0d"  # 12-char prefix of full_head
        mock_run.return_value = MagicMock(returncode=0, stdout=full_head + "\n", stderr="")
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, truncated)
        assert eligible is False  # prefix != full SHA (different lengths)
        assert "SHA mismatch" in detail
        # The truncated expected shows its length
        assert "truncated, 12 chars" in detail
        # The full head shows the ellipsis format
        assert "…" in detail
        assert "(full)" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_mismatch_with_truncated_input_shows_distinguishable_message(self, mock_run: MagicMock) -> None:
        """A mismatch where the expected is a short prefix of a DIFFERENT SHA
        must produce a message where the two values are clearly different."""
        full_head = "72413204ff0dabcdef0123456789abcdef012345"
        wrong_prefix = "deadbeef1234"  # 12 chars, not a prefix of full_head
        mock_run.return_value = MagicMock(returncode=0, stdout=full_head + "\n", stderr="")
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, wrong_prefix)
        assert eligible is False
        assert "SHA mismatch" in detail
        # The truncated expected shows its length
        assert "truncated, 12 chars" in detail
        # The full head shows the ellipsis format
        assert "…" in detail
        assert "(full)" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_full_sha_mismatch_shows_ellipsis(self, mock_run: MagicMock) -> None:
        """A mismatch between two full 40-char SHAs shows the truncated
        ellipsis format on both sides."""
        head_sha = "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
        expected_sha = "ffff9999eeee8888dddd7777cccc6666bbbb4444"
        mock_run.return_value = MagicMock(returncode=0, stdout=head_sha + "\n", stderr="")
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, expected_sha)
        assert eligible is False
        assert "SHA mismatch" in detail
        # Both show ellipsis format
        assert detail.count("…") == 2
        assert "(full)" in detail


class TestCheckNoTestWeakeningGitFailure:
    """check_no_test_weakening must deny when git diff fails."""

    @patch("fast_lane_classify.subprocess.run")
    def test_git_diff_timeout_denies(self, mock_run: MagicMock) -> None:
        import subprocess as _sp

        mock_run.side_effect = _sp.TimeoutExpired(cmd="git", timeout=30)
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert len(violations) == 1
        assert "fail-closed" in violations[0]


# ---------------------------------------------------------------------------
# Guardrail: suspension check (24h circuit breaker)
# ---------------------------------------------------------------------------


class TestCheckSuspensionFailClosed:
    """check_suspension must deny eligibility when it cannot resolve."""

    def test_invalid_repo_denies(self) -> None:
        eligible, detail = check_suspension("invalid repo!; rm -rf /")
        assert eligible is False
        assert "fail-closed" in detail
        assert "invalid repo" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_api_error_denies(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="rate limited", stdout="")
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is False
        assert "fail-closed" in detail
        assert "API error" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_timeout_denies(self, mock_run: MagicMock) -> None:
        import subprocess as _sp

        mock_run.side_effect = _sp.TimeoutExpired(cmd="gh", timeout=15)
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is False
        assert "fail-closed" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_unparseable_timestamp_denies(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="not-a-timestamp\n", stderr="")
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is False
        assert "fail-closed" in detail
        assert "unparseable timestamp" in detail


class TestCheckSuspensionActive:
    """An active suspension must deny eligibility."""

    @patch("fast_lane_classify.datetime")
    @patch("fast_lane_classify.subprocess.run")
    def test_active_suspension_denies(self, mock_run: MagicMock, mock_dt: MagicMock) -> None:
        """A suspension that has not expired must deny eligibility (rc=1)."""
        from datetime import UTC
        from datetime import datetime as _dt

        # Current time: 2026-09-21T12:00:00 UTC
        mock_dt.now.return_value = _dt(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
        mock_dt.fromisoformat = _dt.fromisoformat

        # First call: FAST_LANE_SUSPENDED_UNTIL (active — future)
        # Second call: FAST_LANE_SUSPENSION_REASON
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="2026-09-22T00:00:00\n", stderr=""),
            MagicMock(returncode=0, stdout="critical finding in test file\n", stderr=""),
        ]
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is False
        assert "suspended" in detail.lower()
        assert "2026-09-22" in detail
        assert "critical finding" in detail


class TestCheckSuspensionExpired:
    """An expired suspension must allow eligibility."""

    @patch("fast_lane_classify.datetime")
    @patch("fast_lane_classify.subprocess.run")
    def test_expired_suspension_allows(self, mock_run: MagicMock, mock_dt: MagicMock) -> None:
        """A suspension that has expired must allow eligibility."""
        from datetime import UTC
        from datetime import datetime as _dt

        # Current time: 2026-09-23T00:00:00 UTC (after the suspension window)
        mock_dt.now.return_value = _dt(2026, 9, 23, 0, 0, 0, tzinfo=UTC)
        mock_dt.fromisoformat = _dt.fromisoformat

        mock_run.return_value = MagicMock(returncode=0, stdout="2026-09-22T00:00:00\n", stderr="")
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is True
        assert "expired" in detail.lower()


class TestCheckSuspensionNotSet:
    """A missing variable must allow eligibility (no active suspension)."""

    @patch("fast_lane_classify.subprocess.run")
    def test_missing_variable_allows(self, mock_run: MagicMock) -> None:
        """Variable not found (real gh shape: exit 1, HTTP 404) means no suspension."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: Not Found (HTTP 404)",
        )
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is True
        assert "not set" in detail.lower()

    @patch("fast_lane_classify.subprocess.run")
    def test_missing_variable_lowercase_not_found_allows(self, mock_run: MagicMock) -> None:
        """The lower-case ``not found`` stderr shape is also treated as missing."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is True
        assert "not set" in detail.lower()


class TestCheckSuspensionNaiveTimestamp:
    """A tz-naive stored timestamp must not crash the comparison.

    ``datetime.fromisoformat`` parses tz-naive strings and ``datetime.now(tz=UTC)``
    is aware, so an un-normalised compare raises TypeError.  These tests use the
    real datetime module (no datetime mock) to round-trip that shape.
    """

    @patch("fast_lane_classify.subprocess.run")
    def test_naive_future_timestamp_denies(self, mock_run: MagicMock) -> None:
        """A naive future timestamp is treated as UTC and denies (no TypeError)."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="2999-01-01T00:00:00\n", stderr=""),
            MagicMock(returncode=0, stdout="critical\n", stderr=""),
        ]
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is False
        assert "suspended" in detail.lower()

    @patch("fast_lane_classify.subprocess.run")
    def test_naive_past_timestamp_allows(self, mock_run: MagicMock) -> None:
        """A naive past timestamp is treated as UTC and allows (no TypeError)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="2000-01-01T00:00:00\n",
            stderr="",
        )
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is True
        assert "expired" in detail.lower()


# ---------------------------------------------------------------------------
# Live label check (FAR-1132)
# ---------------------------------------------------------------------------


class TestCheckLabelLive:
    """check_label_live reads the PR's labels from the GitHub API (live)."""

    @patch("fast_lane_classify.subprocess.run")
    def test_label_present_returns_true(self, mock_run: MagicMock) -> None:
        """A PR with fast-lane:test-infra in its live labels returns True."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="agent-generated\nfast-lane:test-infra\n",
            stderr="",
        )
        has_label, detail = check_label_live("farnalabs/modulo", 42)
        assert has_label is True
        assert "present" in detail.lower()

    @patch("fast_lane_classify.subprocess.run")
    def test_label_absent_returns_false(self, mock_run: MagicMock) -> None:
        """A PR without fast-lane:test-infra in its live labels returns False."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="agent-generated\n",
            stderr="",
        )
        has_label, detail = check_label_live("farnalabs/modulo", 42)
        assert has_label is False
        assert "absent" in detail.lower()

    @patch("fast_lane_classify.subprocess.run")
    def test_api_error_fail_closed(self, mock_run: MagicMock) -> None:
        """An API error must deny (fail-closed), not grant label presence."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="rate limited",
        )
        has_label, detail = check_label_live("farnalabs/modulo", 42)
        assert has_label is False
        assert "fail-closed" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_timeout_fail_closed(self, mock_run: MagicMock) -> None:
        """A timeout must deny (fail-closed)."""
        import subprocess as _sp

        mock_run.side_effect = _sp.TimeoutExpired(cmd="gh", timeout=15)
        has_label, detail = check_label_live("farnalabs/modulo", 42)
        assert has_label is False
        assert "fail-closed" in detail

    def test_invalid_repo_fail_closed(self) -> None:
        """An invalid repo slug must deny (fail-closed)."""
        has_label, detail = check_label_live("invalid repo!", 42)
        assert has_label is False
        assert "fail-closed" in detail

    def test_invalid_pr_fail_closed(self) -> None:
        """An invalid PR number must deny (fail-closed)."""
        has_label, detail = check_label_live("farnalabs/modulo", -1)
        assert has_label is False
        assert "fail-closed" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_empty_output_denies(self, mock_run: MagicMock) -> None:
        """Empty stdout (no labels at all) must deny."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        has_label, detail = check_label_live("farnalabs/modulo", 42)
        assert has_label is False
        assert "absent" in detail.lower()


class TestInWindowFollowUpIneligible:
    """Prove that a follow-up PR raised during an active suspension is also
    ineligible for the fast lane — this is the circuit-breaker property.

    Scenario: a fast-lane PR merges and the post-merge review finds a critical
    issue.  A follow-up fix PR is raised.  Because the suspension is still
    active (within the 24h window), the follow-up is denied fast-lane
    eligibility even though its paths may be class-A (test files).  The
    suspension covers the entire window, so the fix cannot chain through the
    fast lane.
    """

    @patch("fast_lane_classify.check_sha_pinning", return_value=(True, "SHA-pinned"))
    @patch("fast_lane_classify.check_no_test_weakening", return_value=(True, []))
    @patch("fast_lane_classify.check_cap", return_value=(True, "cap OK"))
    @patch("fast_lane_classify.check_label_live", return_value=(True, "label present"))
    @patch(
        "fast_lane_classify.check_suspension",
        return_value=(
            False,
            (
                "fast lane suspended until 2026-09-22T00:00:00"
                " - reason: critical finding in prior fast-lane merge"
                " (current time 2026-09-21T12:00:00)"
            ),
        ),
    )
    @patch("fast_lane_classify.subprocess.run")
    def test_followup_during_suspension_is_ineligible(
        self,
        mock_run: MagicMock,
        mock_susp: MagicMock,
        mock_label: MagicMock,
        mock_cap: MagicMock,
        mock_weaken: MagicMock,
        mock_pin: MagicMock,
    ) -> None:
        """A class-A PR with the label is denied when the suspension is active.

        This proves the circuit-breaker property: a follow-up fix raised during
        the 24h suspension window cannot chain through the fast lane, regardless
        of its path classification.
        """
        # gh pr diff returns a class-A path (the follow-up touches test files)
        mock_run.return_value = MagicMock(returncode=0, stdout="backend/tests/unit/test_foo.py\n", stderr="")
        rc = classify_main(
            [
                "--pr-number",
                "99",
                "--head-sha",
                "abc123def456",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
            ]
        )
        assert rc == 1, (
            "Follow-up PR during active suspension must exit 1 (ineligible). "
            "The circuit breaker must prevent the fix from chaining through "
            "the fast lane."
        )
