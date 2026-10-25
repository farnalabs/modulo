"""Unit tests for .github/scripts/pr-review-dedup.py (FAR-2111).

The script is the trigger-side dedup decision for ``pr-review.yml``: it answers
"has this head SHA already been reviewed?" from the PR's reviews plus commits and
prints exactly one ``skip=true`` / ``skip=false`` line.

FAR-2111 made the reviewer start on PR readiness (``pull_request_target``:
``opened`` / ``ready_for_review``) instead of only on product-CI completion, so
the same head SHA now has two dispatch paths. These tests pin the mechanism that
coalesces them:

* the decision is by head SHA and consult NO review identity — a review posted
  by any identity (``modulo-reviewbot`` today) counts, which is what lets the
  late completion dispatch be skipped;
* a first review, and a genuinely new non-merge commit, still dispatch;
* unreadable input fails OPEN (dispatch) — a review must never be dropped on a
  load error.
"""

from __future__ import annotations

import contextlib
import io
import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / ".github" / "scripts" / "pr-review-dedup.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/pr-review-dedup.py")

_loader = SourceFileLoader("pr_review_dedup", str(script_path))
dedup = module_from_spec(spec_from_loader("pr_review_dedup", _loader))
_loader.exec_module(dedup)

HEAD = "a" * 40
PREVIOUS = "b" * 40


def _review(state: str, commit_id: str, login: str = "modulo-reviewbot") -> dict:
    return {
        "state": state,
        "commit_id": commit_id,
        "user": {"login": login},
        "author_association": "MEMBER",
        "body": "a multi-line body\nwith a pipe | and a\nsecond line",
    }


def _commit(sha: str, parents: int = 1) -> dict:
    return {"sha": sha, "parents": [{"sha": "p" * 40}] * parents}


def test_skips_when_the_head_sha_has_already_been_reviewed() -> None:
    """The completion dispatch for a head the early run already reviewed is skipped."""
    skip, reason = dedup.decide([_review("APPROVED", HEAD)], [_commit(HEAD)], HEAD)
    assert skip is True
    assert "same SHA" in reason


def test_the_skip_is_independent_of_who_posted_the_review() -> None:
    """No review identity is consulted, so any reviewer's review coalesces the dispatch.

    ``pr-review.yml`` posts through ``modulo-reviewbot``; the decision must not
    depend on that login (or on any allowlist of it), or a rebrand / token
    rotation would silently start double-reviewing every head SHA.
    """
    for login in ("modulo-reviewbot", "farnalabs", "github-actions[bot]"):
        skip, _ = dedup.decide([_review("CHANGES_REQUESTED", HEAD, login=login)], [_commit(HEAD)], HEAD)
        assert skip is True


def test_dispatches_the_first_review() -> None:
    skip, reason = dedup.decide([], [_commit(HEAD)], HEAD)
    assert skip is False
    assert "first review" in reason


def test_dispatches_when_a_new_non_merge_commit_landed() -> None:
    reviews = [_review("APPROVED", PREVIOUS)]
    commits = [_commit(PREVIOUS), _commit(HEAD)]
    skip, reason = dedup.decide(reviews, commits, HEAD)
    assert skip is False
    assert "non-merge commit" in reason


def test_dispatches_when_a_merge_only_move_followed_a_changes_requested() -> None:
    """A CR is re-reviewed after a merge-only move: it can never wrongly unblock."""
    reviews = [_review("CHANGES_REQUESTED", PREVIOUS)]
    commits = [_commit(PREVIOUS), _commit(HEAD, parents=2)]
    skip, reason = dedup.decide(reviews, commits, HEAD)
    assert skip is False
    assert "CHANGES_REQUESTED" in reason


def test_skips_a_merge_only_move_after_an_approval() -> None:
    reviews = [_review("APPROVED", PREVIOUS)]
    commits = [_commit(PREVIOUS), _commit(HEAD, parents=2)]
    skip, reason = dedup.decide(reviews, commits, HEAD)
    assert skip is True
    assert "merge commits" in reason


def test_dispatches_when_the_reviewed_sha_was_force_pushed_away() -> None:
    reviews = [_review("APPROVED", "c" * 40)]
    skip, reason = dedup.decide(reviews, [_commit(HEAD)], HEAD)
    assert skip is False
    assert "fail open" in reason


def test_main_fails_open_on_unreadable_input(tmp_path: Path) -> None:
    """A load error must dispatch, never silently drop the review."""
    missing = tmp_path / "missing.json"
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = dedup.main(["pr-review-dedup.py", str(missing), str(missing), HEAD])
    assert code == 0
    assert stdout.getvalue().strip() == "skip=false"
    assert "fail open" in stderr.getvalue()


def test_main_prints_one_line_for_a_usable_decision(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews.json"
    commits = tmp_path / "commits.json"
    reviews.write_text(json.dumps([_review("APPROVED", HEAD)]), encoding="utf-8")
    commits.write_text(json.dumps([_commit(HEAD)]), encoding="utf-8")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        code = dedup.main(["pr-review-dedup.py", str(reviews), str(commits), HEAD])
    assert code == 0
    assert stdout.getvalue().strip() == "skip=true"
