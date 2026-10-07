#!/usr/bin/env python3
"""Unit tests for scripts/fast_lane_classify.py.

Run from the repo root:
    uv run --project backend pytest tests/unit/scripts/test_fast_lane_classify.py -q
"""

from __future__ import annotations

import json
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
        ids=[
            "backend-unit-test",
            "backend-integration-test",
            "frontend-unit-test",
            "architecture-test",
            "docs",
            "readme",
            "github-codeowners",
            "github-issue-template",
            "github-pr-template",
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
        ids=[
            "gh-workflow-ci",
            "gh-workflow-deploy",
            "gh-script-freeze-check",
            "scripts-classifier",
            "root-pyproject",
            "backend-pyproject",
            "frontend-package-json",
            "semgrep-rule",
            "pre-commit-config",
            "backend-api-routes",
            "frontend-view",
            "fly-toml",
            "deploy-entrypoint",
            "backend-migration",
            "backend-pipeline-engine",
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

    @patch("fast_lane_classify.subprocess.run")
    def test_diff_too_large_exits_nonzero(self, mock_run: MagicMock) -> None:
        """A PR whose diff exceeds GitHub's 20000-line cap is ineligible (rc=1).

        ``gh pr diff`` fetches the whole diff, so GitHub's HTTP 406 must be
        reported as class-B/neutral — not a classifier error (rc=2), which
        would paint a red ``fast-lane-eligible`` check on a healthy large PR.
        """
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr=(
                "could not find pull request diff: HTTP 406: Sorry, the diff "
                "exceeded the maximum number of lines (20000)"
            ),
        )
        rc = classify_main(
            [
                "--pr-number",
                "1025",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
            ]
        )
        assert rc == 1, "diff-too-large must exit 1 (neutral), not 2 (failure)"

    @patch("fast_lane_classify.subprocess.run")
    def test_unexpected_fetch_error_still_fails_closed(self, mock_run: MagicMock) -> None:
        """A genuine fetch error (not the diff-size cap) still returns rc=2."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
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
        assert rc == 2, "a genuine fetch error must fail closed (rc=2)"

    @patch("fast_lane_classify.subprocess.run")
    def test_diff_fetch_timeout_is_neutral_not_a_failure(self, mock_run: MagicMock) -> None:
        """A transient ``gh pr diff`` timeout must deny (rc=1), never paint red.

        Every other check in the module (check_cap, check_suspension,
        check_no_test_weakening, check_sha_pinning) fails closed to rc=1
        (neutral) on its own timeout. The diff fetch now matches them: a red
        ``fast-lane-eligible`` blocks the PR and dispatches the Branch Fixer
        for an infra blip (observed 2026-10-07 on PR #1373: ``gh pr diff``
        exceeded its 30s bound and the check run concluded ``failure``).
        """
        import subprocess as _sp

        mock_run.side_effect = _sp.TimeoutExpired(cmd="gh", timeout=30)
        rc = classify_main(
            [
                "--pr-number",
                "1373",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
            ]
        )
        assert rc == 1, "a diff-fetch timeout must exit 1 (neutral), not 2 (failure)"

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


class TestCheckCapCounts:
    """check_cap's happy path and counting semantics.

    Only merged PRs whose squash title carries the ``[auto-merge:
    test-infra]`` marker, whose ``mergedAt`` parses, and whose merge epoch
    falls inside the rolling window count toward the cap. Everything else
    (missing/unparseable timestamps, non-marker titles, out-of-window
    merges) is ignored — and the denial only fires once the count is at or
    above the cap.
    """

    def _merged_prs(self, prs: list[dict]) -> str:
        return json.dumps(prs)

    @patch("fast_lane_classify.subprocess.run")
    def test_below_cap_allows(self, mock_run: MagicMock) -> None:
        """One marker PR in-window under a cap of 5 is allowed (cap OK)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._merged_prs(
                [
                    {
                        "number": 100,
                        "title": "[auto-merge: test-infra] fix flaky test",
                        "mergedAt": "2999-01-01T00:00:00+00:00",
                    }
                ]
            ),
            stderr="",
        )
        eligible, detail = check_cap("farnalabs/modulo", 5, 24)
        assert eligible is True
        assert "cap OK" in detail
        assert "1/5" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_at_cap_denies(self, mock_run: MagicMock) -> None:
        """Two marker PRs at a cap of 2 reach the cap and deny."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._merged_prs(
                [
                    {
                        "number": 100,
                        "title": "[auto-merge: test-infra] one",
                        "mergedAt": "2999-01-01T00:00:00+00:00",
                    },
                    {
                        "number": 101,
                        "title": "[auto-merge: test-infra] two",
                        "mergedAt": "2999-01-02T00:00:00+00:00",
                    },
                ]
            ),
            stderr="",
        )
        eligible, detail = check_cap("farnalabs/modulo", 2, 24)
        assert eligible is False
        assert "cap reached" in detail
        assert "2/2" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_non_marker_title_not_counted(self, mock_run: MagicMock) -> None:
        """A merged PR without the auto-merge marker contributes nothing."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._merged_prs(
                [
                    {
                        "number": 100,
                        "title": "fix flaky test",
                        "mergedAt": "2999-01-01T00:00:00+00:00",
                    },
                    {
                        "number": 101,
                        "title": "[auto-merge: test-infra] real one",
                        "mergedAt": "2999-01-02T00:00:00+00:00",
                    },
                ]
            ),
            stderr="",
        )
        eligible, detail = check_cap("farnalabs/modulo", 1, 24)
        assert eligible is False
        assert "cap reached" in detail
        assert "1/1" in detail, "the non-marker merged PR must not be counted"

    @patch("fast_lane_classify.subprocess.run")
    def test_revert_title_still_counted(self, mock_run: MagicMock) -> None:
        """A revert of a fast-lane merge still counts toward the rolling cap.

        ``check_cap`` matches the marker as a case-insensitive substring of
        the squash title (scripts/fast_lane_classify.py:203), so a revert such
        as ``Revert "[auto-merge: test-infra] one"`` itself carries the marker
        and consumes a cap slot -- a merge+revert pair spends two slots of the
        budget. This pins the trap so it is visible, and makes any future
        tightening to exact-title matching a deliberate, test-visible change.
        """
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._merged_prs(
                [
                    {
                        "number": 100,
                        "title": "[auto-merge: test-infra] one",
                        "mergedAt": "2999-01-01T00:00:00+00:00",
                    },
                    {
                        "number": 101,
                        "title": 'Revert "[auto-merge: test-infra] one"',
                        "mergedAt": "2999-01-02T00:00:00+00:00",
                    },
                ]
            ),
            stderr="",
        )
        eligible, detail = check_cap("farnalabs/modulo", 2, 24)
        assert eligible is False
        assert "cap reached" in detail
        assert "2/2" in detail, "the revert title still carries the marker and counts"

    @patch("fast_lane_classify.subprocess.run")
    def test_missing_and_unparseable_merged_at_skipped(self, mock_run: MagicMock) -> None:
        """PRs without a usable mergedAt never count toward the cap."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._merged_prs(
                [
                    {"number": 100, "title": "[auto-merge: test-infra] missing"},
                    {"number": 101, "title": "[auto-merge: test-infra] bad", "mergedAt": "not-a-date"},
                    {
                        "number": 102,
                        "title": "[auto-merge: test-infra] good",
                        "mergedAt": "2999-01-01T00:00:00+00:00",
                    },
                ]
            ),
            stderr="",
        )
        eligible, detail = check_cap("farnalabs/modulo", 2, 24)
        assert eligible is True
        assert "1/2" in detail, "only the parseable in-window marker PR may count"

    @patch("fast_lane_classify.subprocess.run")
    def test_out_of_window_skipped(self, mock_run: MagicMock) -> None:
        """A marker PR merged outside the rolling window does not count."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._merged_prs(
                [
                    {
                        "number": 100,
                        "title": "[auto-merge: test-infra] old merge",
                        "mergedAt": "2000-01-01T00:00:00+00:00",
                    }
                ]
            ),
            stderr="",
        )
        eligible, detail = check_cap("farnalabs/modulo", 1, 24)
        assert eligible is True
        assert "0/1" in detail, "an out-of-window merge must not count toward the cap"

    @patch("fast_lane_classify.subprocess.run")
    def test_zero_cap_denies_even_with_no_prs(self, mock_run: MagicMock) -> None:
        """A zero cap means every candidate is already over the cap."""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        eligible, detail = check_cap("farnalabs/modulo", 0, 24)
        assert eligible is False
        assert "cap reached" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_unparseable_json_denies(self, mock_run: MagicMock) -> None:
        """gh succeeding (exit 0) but emitting non-JSON is still fail-closed."""
        mock_run.return_value = MagicMock(returncode=0, stdout="not-json", stderr="")
        eligible, detail = check_cap("farnalabs/modulo", 5, 24)
        assert eligible is False
        assert "fail-closed" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_gh_token_forwarded_to_env(self, mock_run: MagicMock) -> None:
        """An explicit gh_token must reach the child process environment."""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        check_cap("farnalabs/modulo", 5, 24, gh_token="tok-123")
        assert mock_run.call_args.kwargs["env"]["GH_TOKEN"] == "tok-123"


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


class TestCheckShaPinningMatch:
    """check_sha_pinning's success path: the PR head matches the expected SHA."""

    _HEAD = "72413204ff0dabcdef0123456789abcdef012345"

    @patch("fast_lane_classify.subprocess.run")
    def test_matching_sha_allows(self, mock_run: MagicMock) -> None:
        """A PR head equal to the expected SHA is eligible (SHA-pinned)."""
        mock_run.return_value = MagicMock(returncode=0, stdout=self._HEAD + "\n", stderr="")
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, self._HEAD)
        assert eligible is True
        assert "SHA-pinned" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_matching_sha_case_insensitive(self, mock_run: MagicMock) -> None:
        """SHA comparison is case-insensitive — an upper-case expectation matches."""
        mock_run.return_value = MagicMock(returncode=0, stdout=self._HEAD + "\n", stderr="")
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, self._HEAD.upper())
        assert eligible is True
        assert "SHA-pinned" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_mismatching_sha_still_denies(self, mock_run: MagicMock) -> None:
        """A near-but-not-equal head must remain a denial (stale check)."""
        different = "ffffffff0000abcdef0123456789abcdef012345"
        mock_run.return_value = MagicMock(returncode=0, stdout=different + "\n", stderr="")
        eligible, detail = check_sha_pinning("farnalabs/modulo", 42, self._HEAD)
        assert eligible is False
        assert "SHA mismatch" in detail


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


class TestCheckNoTestWeakeningDiff:
    """check_no_test_weakening's diff analysis: a test change that removes
    coverage must be flagged, and a coverage-neutral change must pass.

    The name-status run feeds the deleted-file check; the full diff run
    feeds the test-function / assert-line / skip-marker / teardown checks.
    """

    def _mock_runs(self, mock_run: MagicMock, name_status: str, diff: str) -> None:
        """Stage the two subprocess runs check_no_test_weakening performs."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=name_status, stderr=""),
            MagicMock(returncode=0, stdout=diff, stderr=""),
        ]

    @patch("fast_lane_classify.subprocess.run")
    def test_clean_changes_allowed(self, mock_run: MagicMock) -> None:
        """An added test function with an assert — no coverage loss."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            "+def test_new():\n+    assert value == expected\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is True
        assert violations == []

    @patch("fast_lane_classify.subprocess.run")
    def test_clean_added_test_file_allowed(self, mock_run: MagicMock) -> None:
        """An added test file with one test function and one assert is allowed."""
        self._mock_runs(
            mock_run,
            "A\tbackend/tests/unit/test_new_module.py\n",
            (
                "--- /dev/null\n"
                "+++ b/backend/tests/unit/test_new_module.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+import pytest\n"
                "+def test_new():\n"
                "+    assert True\n"
            ),
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is True
        assert violations == []

    @patch("fast_lane_classify.subprocess.run")
    def test_file_headers_not_counted_even_when_path_has_assert(self, mock_run: MagicMock) -> None:
        """``+++``/``---`` headers are never assert lines, even when the file
        path itself contains the substring ``assert``.

        The path ``test_assert_new.py`` makes BOTH diff headers contain
        ``assert``. A net-zero assert swap (one removed, one added) would hide
        a regression in the ``+++``/``---`` exclusion, because both counts
        would rise together. This fixture removes two content asserts and adds
        one, so the violation message must report the exact content counts
        ``-2 removed, +1 added``. If the header exclusion regressed, the
        message would become ``-3 removed, +2 added`` and this assertion
        fails.
        """
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_assert_new.py\n",
            (
                "--- a/backend/tests/unit/test_assert_new.py\n"
                "+++ b/backend/tests/unit/test_assert_new.py\n"
                "@@ -1,3 +1,2 @@\n"
                " def test_keep():\n"
                "-    assert old_a == 1\n"
                "-    assert old_b == 2\n"
                "+    assert new_a == 1\n"
            ),
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert len(violations) == 1, "only the assert-count violation should fire"
        assert "-2 removed" in violations[0]
        assert "+1 added" in violations[0]
        assert "net -1" in violations[0]

    @patch("fast_lane_classify.subprocess.run")
    def test_assert_removal_flagged(self, mock_run: MagicMock) -> None:
        """Removing an assert line without adding one is a coverage loss."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            "-def test_old():\n-    assert old_value == 1\n+def test_new():\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("assert-line count decreased" in v for v in violations)
        assert "net -1" in violations[0]

    @patch("fast_lane_classify.subprocess.run")
    def test_balanced_assert_swap_allowed(self, mock_run: MagicMock) -> None:
        """Moving an assert (one removed, one added) is not a decrease."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            "-    assert old_value == 1\n+    assert new_value == 1\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is True
        assert violations == []

    @patch("fast_lane_classify.subprocess.run")
    def test_function_removal_flagged(self, mock_run: MagicMock) -> None:
        """Removing a test function without adding one is a coverage loss."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            "-def test_old():\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("test-function count decreased" in v for v in violations)
        assert "net -1" in violations[0]

    @patch("fast_lane_classify.subprocess.run")
    def test_function_renamed_not_flagged(self, mock_run: MagicMock) -> None:
        """A one-for-one function replacement is coverage-neutral."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            "-def test_old():\n+def test_new():\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is True
        assert violations == []

    @patch("fast_lane_classify.subprocess.run")
    def test_added_skip_marker_flagged(self, mock_run: MagicMock) -> None:
        """Any added skip/xfail/skipif marker is a coverage loss."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            '+    @pytest.mark.xfail(reason="known broken")\n',
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("skip/xfail/skipif" in v for v in violations)

    @patch("fast_lane_classify.subprocess.run")
    def test_added_pytest_skip_call_flagged(self, mock_run: MagicMock) -> None:
        """A bare ``pytest.skip(...)`` added to a test body is flagged."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            '+        pytest.skip("not implemented")\n',
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("skip/xfail/skipif" in v for v in violations)

    @patch("fast_lane_classify.subprocess.run")
    def test_deleted_test_file_flagged(self, mock_run: MagicMock) -> None:
        """Deleting a whole test file is a coverage loss."""
        self._mock_runs(
            mock_run,
            "D\tbackend/tests/unit/test_gone.py\n",
            "",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("deleted test files" in v for v in violations)
        assert "backend/tests/unit/test_gone.py" in violations[0]

    @patch("fast_lane_classify.subprocess.run")
    def test_malformed_name_status_line_ignored(self, mock_run: MagicMock) -> None:
        """A name-status line without a tab separator must not be read as a
        deletion (and cannot paint a false 'deleted test file' violation)."""
        self._mock_runs(
            mock_run,
            "not-a-real-status-line\nM\tbackend/tests/unit/test_foo.py\n",
            "+def test_new():\n+    assert True\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is True
        assert violations == []

    @patch("fast_lane_classify.subprocess.run")
    def test_removed_teardown_flagged(self, mock_run: MagicMock) -> None:
        """Removed fixture teardown (yield) is a coverage loss."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            "-    yield\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("fixture teardown" in v for v in violations)

    @patch("fast_lane_classify.subprocess.run")
    def test_removed_addfinalizer_flagged(self, mock_run: MagicMock) -> None:
        """Removed ``addfinalizer`` teardown is a coverage loss."""
        self._mock_runs(
            mock_run,
            "M\tbackend/tests/unit/test_foo.py\n",
            "-    request.addfinalizer(cleanup)\n",
        )
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("fixture teardown" in v for v in violations)

    @patch("fast_lane_classify.subprocess.run")
    def test_content_fetch_failure_fails_closed(self, mock_run: MagicMock) -> None:
        """Failure on the second (diff-content) run fails closed too."""
        import subprocess as _sp

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="M\tbackend/tests/unit/test_foo.py\n", stderr=""),
            _sp.TimeoutExpired(cmd="git", timeout=30),
        ]
        eligible, violations = check_no_test_weakening(Path("/tmp"), "origin/main", "HEAD")
        assert eligible is False
        assert any("fail-closed" in v for v in violations)
        assert any("could not fetch diff content" in v for v in violations)


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


class TestCheckSuspensionEmptyVariable:
    """A set-but-empty suspension variable means no active suspension."""

    @patch("fast_lane_classify.subprocess.run")
    def test_empty_variable_allows(self, mock_run: MagicMock) -> None:
        """gh succeeds with an empty value — treated as 'no active suspension'."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is True
        assert "variable empty" in detail.lower()

    @patch("fast_lane_classify.subprocess.run")
    def test_whitespace_variable_allows(self, mock_run: MagicMock) -> None:
        """A value made up only of whitespace strips to empty and allows."""
        mock_run.return_value = MagicMock(returncode=0, stdout="   \n", stderr="")
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is True
        assert "variable empty" in detail.lower()


class TestCheckSuspensionPermissionError:
    """A token that cannot read variables is a config fault, not an unknown state.

    The Actions integration token returns HTTP 403 "Resource not accessible by
    integration" for the variables API, which fail-closed every fast-lane
    candidate (observed on PR #907/#904).  The denial must stay fail-closed but
    name the actual cause so the missing token permission is diagnosable.
    """

    @patch("fast_lane_classify.subprocess.run")
    def test_integration_token_403_denies_with_config_cause(self, mock_run: MagicMock) -> None:
        """The real CI shape: gh exit 1, 403 Resource not accessible by integration."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is False
        assert "fail-closed" in detail
        assert "token cannot read repository variables" in detail
        assert "MODULO_REVIEWBOT_TOKEN" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_forbidden_403_denies_with_config_cause(self, mock_run: MagicMock) -> None:
        """A generic 403 Forbidden is likewise surfaced as a variables-read fault."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: Forbidden (HTTP 403)",
        )
        eligible, detail = check_suspension("farnalabs/modulo")
        assert eligible is False
        assert "fail-closed" in detail
        assert "token cannot read repository variables" in detail

    @patch("fast_lane_classify.subprocess.run")
    def test_permission_error_not_misread_as_missing_variable(self, mock_run: MagicMock) -> None:
        """A 403 must NOT be treated as "variable absent" (which would allow)."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )
        eligible, _detail = check_suspension("farnalabs/modulo")
        assert eligible is False


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
