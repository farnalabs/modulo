"""Unit tests for the PR Reviewer trigger-side dedup decision script.

The script lives at .github/scripts/pr-review-dedup.py and is invoked by the
ci.yml notify-pr-review job to decide whether the PR Reviewer webhook should
fire for a given head. It must be tested because the decision is subtle:
multi-line review bodies and pipe characters in body text break naive
line-oriented shell parsing, and ANY CHANGES_REQUESTED followed by a
merge-only head move must dispatch a re-review (skipping would deadlock the
PR in CHANGES_REQUESTED).

Run: pytest backend/tests/unit/scripts/test_pr_review_dedup.py
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[4] / ".github" / "scripts" / "pr-review-dedup.py").resolve()

HEAD = "abcdef1234567890abcdef1234567890abcdef12"


def write_json(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def run_script(tmp_path, reviews, commits, head=HEAD):
    reviews_file = write_json(tmp_path, "reviews.json", reviews)
    commits_file = write_json(tmp_path, "commits.json", commits)
    return subprocess.run(  # noqa: S603 — trusted input (sys.executable + test fixtures)
        [sys.executable, str(SCRIPT), reviews_file, commits_file, head],
        capture_output=True,
        text=True,
        check=False,
    )


def make_review(commit_id, state, body=""):
    return {"commit_id": commit_id, "state": state, "body": body, "submitted_at": "2026-08-13T00:00:00Z"}


def make_commit(sha, parents):
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


def make_history():
    """Oldest -> newest. HEAD is the last commit (a merge of main)."""
    c1 = make_commit("a" * 40, ["0" * 40])
    c2 = make_commit("b" * 40, ["a" * 40])
    merge = make_commit("m" * 40, ["b" * 40, "9" * 40])
    return [c1, c2, merge]


def test_no_prior_review_dispatches(tmp_path):
    result = run_script(tmp_path, [], make_history())
    assert "skip=false" in result.stdout


def test_same_sha_skips(tmp_path):
    reviews = [make_review(HEAD, "CHANGES_REQUESTED", "looks good")]  # single-line body
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=true" in result.stdout


def test_same_sha_skips_with_multiline_body(tmp_path):
    # The regression this script fixes: a multi-line review body must not
    # corrupt the parsed commit SHA (cut -d'|' is line-oriented and bleeds
    # continuation lines into field 1, so the same-SHA guard never matched).
    reviews = [make_review(HEAD, "CHANGES_REQUESTED", "Line one\nLine two\nLine three")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=true" in result.stdout


def test_same_sha_skips_with_pipe_in_body(tmp_path):
    # Pipe characters inside the body must not break field parsing either.
    reviews = [make_review(HEAD, "CHANGES_REQUESTED", "a | b | c")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=true" in result.stdout


def test_fix_push_dispatches(tmp_path):
    # A non-merge commit (real fix) after the last review always dispatches.
    reviews = [make_review("b" * 40, "CHANGES_REQUESTED", "findings")]
    commits = [*make_history(), make_commit("f" * 40, ["m" * 40])]  # new fix commit on top
    result = run_script(tmp_path, reviews, commits)
    assert "skip=false" in result.stdout


def test_merge_only_approved_skips(tmp_path):
    # Head moved only via merge commits and prior review was APPROVED: no new
    # code, approval stands, skip.
    reviews = [make_review("b" * 40, "APPROVED", "approved")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=true" in result.stdout


def test_merge_only_code_cr_dispatches(tmp_path):
    # Head moved only via merge commits after a code-findings
    # CHANGES_REQUESTED: fail open. The merge may have resolved the CI
    # failure or realigned a stale base, so a re-review is required - a
    # re-review can only re-confirm the CR or approve, never wrongly unblock.
    reviews = [make_review("b" * 40, "CHANGES_REQUESTED", "delete_pipeline has no team check")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=false" in result.stdout


def test_merge_only_conflict_cr_dispatches(tmp_path):
    # Head moved only via merge commits after a merge-conflict
    # CHANGES_REQUESTED: the merge may have resolved it, so a re-review is
    # required or the PR deadlocks. (Subsumed by the general fail-open rule
    # above but kept as an explicit case.)
    reviews = [make_review("b" * 40, "CHANGES_REQUESTED", "PR has merge conflicts with main")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=false" in result.stdout


def test_merge_only_cr_without_conflict_text_dispatches(tmp_path):
    # Regression for PRs #1263/#1271: a CHANGES_REQUESTED whose body does NOT
    # mention a merge conflict, followed by a merge-only head move, must STILL
    # dispatch. The old rule only failed open on bodies containing "merge
    # conflict", which suppressed the re-review and deadlocked both PRs in
    # CHANGES_REQUESTED with green CI.
    reviews = [make_review("b" * 40, "CHANGES_REQUESTED", "some non-conflict finding")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=false" in result.stdout


def test_merge_only_commented_dispatches(tmp_path):
    # COMMENTED / DISMISSED prior states are not decisive - fail open.
    reviews = [make_review("b" * 40, "COMMENTED", "some comment")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=false" in result.stdout


def test_force_push_rewrite_dispatches(tmp_path):
    # Last review SHA no longer in commit history (force-push rewrote it):
    # cannot prove nothing changed, fail open.
    reviews = [make_review("z" * 40, "APPROVED", "approved")]
    result = run_script(tmp_path, reviews, make_history())
    assert "skip=false" in result.stdout


def test_api_failure_fails_open(tmp_path):
    # Malformed JSON input -> fail open (dispatch).
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    result = subprocess.run(  # noqa: S603 — trusted input (sys.executable + test fixtures)
        [sys.executable, str(SCRIPT), str(bad), str(bad), HEAD],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "skip=false" in result.stdout
