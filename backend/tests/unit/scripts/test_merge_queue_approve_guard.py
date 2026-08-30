"""Unit tests for .github/scripts/merge-queue-approve-guard.py (FAR-455).

These prove the SHA-pin TOCTOU guard:
  * merges `gh api --paginate` output correctly across multiple pages
    (the prior bash implementation failed OPEN on the >100-item path), and
  * blocks when a non-merge commit lands after the approval, while keeping the
    approval valid across merge-only head moves.
"""

from __future__ import annotations

import contextlib
import io
import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / ".github" / "scripts" / "merge-queue-approve-guard.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/merge-queue-approve-guard.py")

_loader = SourceFileLoader("merge_queue_approve_guard", str(script_path))
guard = module_from_spec(spec_from_loader("merge_queue_approve_guard", _loader))
_loader.exec_module(guard)


def _approved(submitted_at: str, commit_id: str = "abc123") -> dict:
    return {
        "state": "APPROVED",
        "submitted_at": submitted_at,
        "commit_id": commit_id,
    }


def _commit(sha: str, parents: int, committer_date: str) -> dict:
    return {
        "sha": sha,
        "parents": [{"sha": f"p{i}"} for i in range(parents)],
        "commit": {"committer": {"date": committer_date}},
    }


APPROVE_AT = "2026-08-30T08:00:00Z"
# A commit committed strictly after the approval.
AFTER = "2026-08-30T09:00:00Z"
# A commit committed before the approval (pre-existing).
BEFORE = "2026-08-30T07:00:00Z"


def test_no_approved_review_is_stale():
    verdict, reason = guard.decide([{"state": "CHANGES_REQUESTED"}], [], None)
    assert verdict == "stale"
    assert "no APPROVED" in reason


def test_merge_only_head_moves_are_ok():
    # All commits are merges (>=2 parents) and committed after the approval:
    # the approval stays valid (policy treats merge-only moves as not invalidating).
    commits = [
        _commit("m1", 2, AFTER),
        _commit("m2", 2, AFTER),
    ]
    verdict, _reason = guard.decide([_approved(APPROVE_AT)], commits, "head")
    assert verdict == "ok"


def test_post_approval_non_merge_commit_is_stale():
    commits = [
        _commit("fix", 1, AFTER),  # non-merge, after approval
        _commit("m1", 2, AFTER),  # merge, after approval
    ]
    verdict, reason = guard.decide([_approved(APPROVE_AT)], commits, "head")
    assert verdict == "stale"
    assert "1 non-merge" in reason


def test_missing_commit_date_fails_closed():
    # A non-merge commit with a MISSING committer date cannot be verified, so it
    # must fail closed (stale) rather than be silently treated as valid.
    commits = [
        {
            "sha": "fix",
            "parents": [{"sha": "p0"}],
            "commit": {"committer": {}},  # no "date"
        },
    ]
    verdict, reason = guard.decide([_approved(APPROVE_AT)], commits, "head")
    assert verdict == "stale"
    assert "non-merge" in reason

    # Also cover a commit with a literal None committer date.
    commits_none = [
        {
            "sha": "fix",
            "parents": [{"sha": "p0"}],
            "commit": {"committer": {"date": None}},
        },
    ]
    verdict2, _reason2 = guard.decide([_approved(APPROVE_AT)], commits_none, "head")
    assert verdict2 == "stale"


def test_malformed_commit_date_fails_closed():
    # A non-merge commit with a malformed (unparseable) committer date cannot be
    # verified, so it must fail closed (stale) rather than be silently skipped.
    commits = [
        {
            "sha": "fix",
            "parents": [{"sha": "p0"}],
            "commit": {"committer": {"date": "not-a-timestamp"}},
        },
    ]
    verdict, reason = guard.decide([_approved(APPROVE_AT)], commits, "head")
    assert verdict == "stale"
    assert "non-merge" in reason


def test_pre_approval_non_merge_commit_is_ok():
    commits = [
        _commit("old", 1, BEFORE),  # non-merge but before approval
        _commit("m1", 2, AFTER),
    ]
    verdict, _reason = guard.decide([_approved(APPROVE_AT)], commits, "head")
    assert verdict == "ok"


def test_last_approved_review_is_used():
    # An older approval then a newer CHANGES_REQUESTED-then-APPROVED; the LATEST
    # APPROVED governs. A stale non-merge commit after the latest approval blocks.
    reviews = [
        _approved(BEFORE, "oldhead"),
        {"state": "CHANGES_REQUESTED", "submitted_at": BEFORE, "commit_id": "x"},
        _approved(APPROVE_AT, "newhead"),
    ]
    commits = [_commit("fix", 1, AFTER)]
    verdict, _reason = guard.decide(reviews, commits, "head")
    assert verdict == "stale"


def test_single_page_parse(tmp_path):
    reviews_file = tmp_path / "reviews.json"
    commits_file = tmp_path / "commits.json"
    reviews_file.write_text(json.dumps([_approved(APPROVE_AT)]))
    commits_file.write_text(json.dumps([_commit("fix", 1, AFTER)]))
    reviews = guard.load_concatenated(str(reviews_file))
    commits = guard.load_concatenated(str(commits_file))
    assert len(reviews) == 1
    assert len(commits) == 1
    verdict, _ = guard.decide(reviews, commits, "head")
    assert verdict == "stale"


def test_multi_page_concatenation_merges_all_pages(tmp_path):
    # Simulate `gh api --paginate` output: two JSON documents concatenated, the
    # stale commit lives ONLY on the second page. The original bash bug saw only
    # the first page and failed OPEN.
    reviews_file = tmp_path / "reviews.json"
    commits_file = tmp_path / "commits.json"
    page1 = [_commit("m1", 2, AFTER), _commit("m2", 2, AFTER)]
    page2 = [_commit("m3", 2, AFTER), _commit("fix", 1, AFTER)]
    reviews_file.write_text(json.dumps([_approved(APPROVE_AT)]))
    commits_file.write_text(json.dumps(page1) + json.dumps(page2))

    reviews = guard.load_concatenated(str(reviews_file))
    commits = guard.load_concatenated(str(commits_file))
    assert len(commits) == 4  # both pages flattened, not just the first
    verdict, reason = guard.decide(reviews, commits, "head")
    assert verdict == "stale"
    assert "1 non-merge" in reason


def test_multi_page_reviews_last_approved_across_pages(tmp_path):
    # Two pages of reviews; the LAST APPROVED is on page 2. Must be selected.
    reviews_file = tmp_path / "reviews.json"
    commits_file = tmp_path / "commits.json"
    page1 = [{"state": "APPROVED", "submitted_at": BEFORE, "commit_id": "old"}]
    page2 = [_approved(APPROVE_AT, "new")]
    reviews_file.write_text(json.dumps(page1) + json.dumps(page2))
    commits_file.write_text(json.dumps([_commit("fix", 1, AFTER)]))
    reviews = guard.load_concatenated(str(reviews_file))
    commits = guard.load_concatenated(str(commits_file))
    assert len(reviews) == 2
    verdict, _ = guard.decide(reviews, commits, "head")
    assert verdict == "stale"  # governed by page-2 approval


def test_main_cli_emits_ok_and_stale(tmp_path):
    reviews_file = tmp_path / "reviews.json"
    commits_file = tmp_path / "commits.json"
    reviews_file.write_text(json.dumps([_approved(APPROVE_AT)]))
    # ok case: merge-only
    commits_file.write_text(json.dumps([_commit("m1", 2, AFTER)]))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = guard.main(["script", str(reviews_file), str(commits_file), "head"])
    assert rc == 0
    assert out.getvalue().startswith("ok|")

    # stale case: post-approval non-merge
    commits_file.write_text(json.dumps([_commit("fix", 1, AFTER)]))
    out2 = io.StringIO()
    with contextlib.redirect_stdout(out2):
        rc2 = guard.main(["script", str(reviews_file), str(commits_file), "head"])
    assert rc2 == 1
    assert out2.getvalue().startswith("stale|")


def test_main_cli_missing_args_returns_usage():
    assert guard.main(["only-one-arg"]) == 2
