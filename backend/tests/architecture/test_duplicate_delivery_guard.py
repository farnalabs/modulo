"""Architecture guard: the duplicate-delivery guard must actually detect a second
open PR for one Linear ticket (FAR-1883).

On 2026-10-20 PR #2017 and PR #2029 both carried ``FAR-1883`` in their titles
and were both driven at ``main``. #2029's branch was cut from #2017's head, so
its diff re-carried the whole feature including the same Alembic migration;
when ``main`` took that migration's numeric prefix the merge queue saw two
heads and aborted a batch of ten approved PRs for ~4 hours.

These tests pin the *detection*, not the YAML: the parsing, the collision rule
and the ``[partial]`` chained-sweep exemption are exercised as pure functions,
and the CLI wiring is exercised with the ``gh``-facing functions replaced by
recording fakes. A "clean" result from an extractor that matched nothing is the
exact failure mode this guard exists to prevent, so every negative case also
asserts the ticket WAS extracted for its sibling positive case.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_duplicate_delivery.py"

_loader = SourceFileLoader("check_duplicate_delivery", str(SCRIPT_PATH))
_guard = module_from_spec(spec_from_loader(_loader.name, _loader))
# Register before exec: the module defines a dataclass, and dataclasses resolve
# their defining module through ``sys.modules`` while processing the class.
sys.modules[_loader.name] = _guard
_loader.exec_module(_guard)

PR = _guard.PullRequest


# --- ticket extraction ------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("feat(FAR-1883): pipeline queue timeout", "FAR-1883"),
        ("fix(FAR-1883) [partial]: first slice", "FAR-1883"),
        ("feat(far-1883): lowercase identifier", "FAR-1883"),
        ("FAR-1883", "FAR-1883"),
        ("feat(FAR-1883,FAR-1900): two tickets, first wins", "FAR-1883"),
        ("feat(FAR-0042): leading zeros normalise", "FAR-42"),
    ],
)
def test_extract_ticket_reads_the_identifier(title: str, expected: str) -> None:
    assert _guard.extract_ticket(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "docs: no ticket here",
        "fix(bugfix): FAR is mentioned without a number",
        "feat(XFAR-1883): glued prefix is not a ticket",
        "feat(FAR-): no digits",
    ],
)
def test_extract_ticket_returns_none_without_a_ticket(title: str) -> None:
    assert _guard.extract_ticket(title) is None


# --- the [partial] marker ---------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("fix(FAR-366) [partial]: sonar smells", True),
        ("fix(FAR-366) [PARTIAL]: uppercase marker", True),
        ("feat(FAR-366): final slice", False),
    ],
)
def test_is_partial(title: str, expected: bool) -> None:
    assert _guard.is_partial(title) is expected


# --- the collision rule -----------------------------------------------------


def test_matching_titles_collide() -> None:
    subject = PR(2029, "feat(FAR-1883): leftovers")
    others = [PR(2017, "feat(FAR-1883): pipeline queue timeout")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert _guard.extract_ticket(subject.title) == "FAR-1883"
    assert [dup.number for dup in duplicates] == [2017]


def test_case_differing_identifiers_collide() -> None:
    subject = PR(2029, "feat(far-1883): leftovers")
    others = [PR(2017, "feat(FAR-1883): pipeline queue timeout")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert [dup.number for dup in duplicates] == [2017]


def test_a_pr_is_never_compared_against_itself() -> None:
    subject = PR(2029, "feat(FAR-1883): leftovers")
    others = [PR(2029, "feat(FAR-1883): leftovers")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert not duplicates


def test_a_different_ticket_does_not_collide() -> None:
    subject = PR(2029, "feat(FAR-1883): leftovers")
    others = [PR(2017, "feat(FAR-1900): something else")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert _guard.extract_ticket(others[0].title) == "FAR-1900"
    assert not duplicates


def test_a_title_without_a_ticket_never_collides() -> None:
    subject = PR(2029, "chore: tidy up")
    others = [PR(2017, "feat(FAR-1883): pipeline queue timeout")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert _guard.extract_ticket(others[0].title) == "FAR-1883"
    assert not duplicates


def test_a_partial_counterpart_is_a_legal_chain_not_a_duplicate() -> None:
    subject = PR(2029, "feat(FAR-366): final slice")
    others = [PR(2017, "fix(FAR-366) [partial]: wave 5 slice 1")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert _guard.extract_ticket(others[0].title) == "FAR-366"
    assert not duplicates


def test_a_partial_subject_is_a_legal_chain_not_a_duplicate() -> None:
    subject = PR(2029, "fix(FAR-366) [partial]: wave 5 slice 2")
    others = [PR(2017, "feat(FAR-366): base slice")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert _guard.extract_ticket(subject.title) == "FAR-366"
    assert not duplicates


def test_two_partial_prs_are_still_a_chain() -> None:
    subject = PR(2029, "fix(FAR-366) [partial]: slice 2")
    others = [PR(2017, "fix(FAR-366) [partial]: slice 1")]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert not duplicates


def test_a_stacked_pair_without_partial_markers_collides() -> None:
    subject = PR(2029, "feat(FAR-1883): docs and UI leftovers")
    others = [
        PR(2017, "feat(FAR-1883): pipeline queue timeout"),
        PR(2031, "feat(FAR-1900): unrelated"),
    ]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert [dup.number for dup in duplicates] == [2017]


def test_collisions_are_ordered_by_pr_number() -> None:
    subject = PR(2100, "feat(FAR-1883): third attempt")
    others = [
        PR(2040, "feat(FAR-1883): second attempt"),
        PR(2017, "feat(FAR-1883): first attempt"),
    ]

    duplicates = _guard.find_duplicate_deliveries(subject, others)

    assert [dup.number for dup in duplicates] == [2017, 2040]


def test_annotation_text_escapes_percent() -> None:
    """A percent sign in a PR title would otherwise decode as an escape."""
    assert _guard.escape_annotation("cut cost 50%") == "cut cost 50%25"


# --- the comment and its idempotency key -----------------------------------


def test_comment_names_both_prs_and_their_titles() -> None:
    subject = PR(2029, "feat(FAR-1883): docs and UI leftovers")
    counterpart = PR(2017, "feat(FAR-1883): pipeline queue timeout")

    body = _guard.render_comment(subject, counterpart, "FAR-1883")

    assert "#2029" in body
    assert "#2017" in body
    assert "docs and UI leftovers" in body
    assert "pipeline queue timeout" in body
    assert "FAR-1883" in body
    assert "[partial]" in body


def test_comment_carries_the_counterpart_marker() -> None:
    subject = PR(2029, "feat(FAR-1883): leftovers")
    counterpart = PR(2017, "feat(FAR-1883): queue timeout")

    body = _guard.render_comment(subject, counterpart, "FAR-1883")

    assert _guard.comment_marker(2017) in body


def test_already_reported_matches_only_the_same_counterpart() -> None:
    subject = PR(2029, "feat(FAR-1883): leftovers")
    body = _guard.render_comment(subject, PR(2017, "feat(FAR-1883): queue timeout"), "FAR-1883")

    assert _guard.already_reported([body], 2017)
    assert not _guard.already_reported([body], 2040)
    assert not _guard.already_reported([], 2017)


# --- gh-facing parsing ------------------------------------------------------


def test_list_open_prs_parses_number_and_title(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {"number": 2017, "title": "feat(FAR-1883): pipeline queue timeout"},
        {"number": 2029, "title": "feat(FAR-1883): leftovers"},
    ]

    def fake_run_gh(args, *, expect_failure=False):
        assert "--base" in args
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(_guard, "_run_gh", fake_run_gh)

    prs = _guard.list_open_prs("farnalabs/modulo", "main")

    assert [pr.number for pr in prs] == [2017, 2029]
    assert prs[0].title == "feat(FAR-1883): pipeline queue timeout"


# --- CLI wiring: warn, comment once, never fail on a collision -------------


class _FakeGh:
    """Records every mutating call the CLI makes against a fake repo state."""

    def __init__(self, open_prs: list, existing: list[str] | None = None) -> None:
        self.open_prs = open_prs
        self.comments: list[str] = list(existing or [])
        self.posts: list[tuple[int, str]] = []
        self.labels: list[tuple[str, int]] = []
        self.label_created = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_guard, "list_open_prs", lambda repo, base: list(self.open_prs))
        monkeypatch.setattr(_guard, "list_comment_bodies", lambda repo, number: list(self.comments))
        monkeypatch.setattr(_guard, "ensure_label", self._ensure_label)
        monkeypatch.setattr(_guard, "add_label", self._add_label)
        monkeypatch.setattr(_guard, "post_comment", self._post_comment)

    def _ensure_label(self, repo: str) -> bool:
        self.label_created = True
        return True

    def _add_label(self, repo: str, number: int) -> bool:
        self.labels.append((repo, number))
        return True

    def _post_comment(self, repo: str, number: int, body: str) -> bool:
        self.posts.append((number, body))
        self.comments.append(body)
        return True


_ARGS = [
    "--repo",
    "farnalabs/modulo",
    "--number",
    "2029",
    "--title",
    "feat(FAR-1883): docs and UI leftovers",
    "--base",
    "main",
]


def test_collision_warns_comments_and_labelled_but_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGh([PR(2017, "feat(FAR-1883): pipeline queue timeout")])
    fake.install(monkeypatch)

    exit_code = _guard.main(_ARGS)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "::warning::" in output
    assert "#2017" in output
    assert len(fake.posts) == 1
    assert fake.posts[0][0] == 2029
    assert _guard.comment_marker(2017) in fake.posts[0][1]
    assert fake.labels == [("farnalabs/modulo", 2029)]
    assert fake.label_created


def test_reopening_the_same_pr_does_not_re_post(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh([PR(2017, "feat(FAR-1883): pipeline queue timeout")])
    fake.install(monkeypatch)

    assert _guard.main(_ARGS) == 0
    assert len(fake.posts) == 1

    # The edited/reopened event fires again with the comment already present.
    assert _guard.main(_ARGS) == 0
    assert len(fake.posts) == 1


def test_a_second_counterpart_gets_its_own_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh(
        [
            PR(2017, "feat(FAR-1883): pipeline queue timeout"),
            PR(2040, "feat(FAR-1883): second attempt"),
        ]
    )
    fake.install(monkeypatch)

    assert _guard.main(_ARGS) == 0
    assert len(fake.posts) == 2
    assert _guard.comment_marker(2017) in fake.posts[0][1]
    assert _guard.comment_marker(2040) in fake.posts[1][1]


def test_no_collision_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh([PR(2017, "feat(FAR-1900): something else")])
    fake.install(monkeypatch)

    exit_code = _guard.main(_ARGS)

    assert exit_code == 0
    assert not fake.posts
    assert not fake.labels


def test_a_title_without_a_ticket_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh([PR(2017, "feat(FAR-1883): pipeline queue timeout")])
    fake.install(monkeypatch)

    exit_code = _guard.main(
        ["--repo", "farnalabs/modulo", "--number", "2029", "--title", "chore: tidy up", "--base", "main"]
    )

    assert exit_code == 0
    assert not fake.posts


def test_a_partial_chain_posts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh([PR(2017, "fix(FAR-366) [partial]: wave 5 slice 1")])
    fake.install(monkeypatch)

    exit_code = _guard.main(
        [
            "--repo",
            "farnalabs/modulo",
            "--number",
            "2029",
            "--title",
            "feat(FAR-366): final slice",
            "--base",
            "main",
        ]
    )

    assert exit_code == 0
    assert not fake.posts


def test_unreadable_open_prs_fails_the_guard_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard that cannot see its input must not report a silent green."""

    def broken(repo: str, base: str) -> list:
        raise _guard.GuardError("gh pr list exited 1")

    monkeypatch.setattr(_guard, "list_open_prs", broken)

    exit_code = _guard.main(_ARGS)

    assert exit_code == _guard.EXIT_GUARD_BROKEN


def test_dry_run_reports_without_posting(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = _FakeGh([PR(2017, "feat(FAR-1883): pipeline queue timeout")])
    fake.install(monkeypatch)

    exit_code = _guard.main([*_ARGS, "--dry-run"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert not fake.posts
    assert not fake.labels
    assert _guard.comment_marker(2017) in output


def test_an_unpostable_comment_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only token (fork PR) cannot comment; the guard still exits 0."""

    class _Unpostable(_FakeGh):
        def _post_comment(self, repo: str, number: int, body: str) -> bool:
            return False

        def _add_label(self, repo: str, number: int) -> bool:
            return False

    fake = _Unpostable([PR(2017, "feat(FAR-1883): pipeline queue timeout")])
    fake.install(monkeypatch)

    exit_code = _guard.main(_ARGS)

    assert exit_code == 0
    assert not fake.posts


# --- S8705 taint barriers: operator-supplied argv values are regex-bounded ---


class TestArgTaintBarriers:
    """Bound --repo / --base / --number before they reach a subprocess argv."""

    @pytest.mark.parametrize("repo", ["farnalabs/modulo", "farnalabs/modulo-new", "a/b"])
    def test_safe_repo_accepts_valid(self, repo: str) -> None:
        assert _guard._safe_repo(repo) == repo

    @pytest.mark.parametrize(
        "repo",
        ["", "no-slash", "--upload-pack=evil", "owner/repo; rm -rf /", "owner/repo --body-file /etc/passwd"],
    )
    def test_safe_repo_rejects_injection(self, repo: str) -> None:
        with pytest.raises(_guard.GuardError):
            _guard._safe_repo(repo)

    @pytest.mark.parametrize("base", ["main", "release/1.2", "feat/FAR-1883"])
    def test_safe_base_accepts_valid(self, base: str) -> None:
        assert _guard._safe_base(base) == base

    @pytest.mark.parametrize("base", ["", "-x", "main; rm -rf /", "main --upload-pack=evil"])
    def test_safe_base_rejects_injection(self, base: str) -> None:
        with pytest.raises(_guard.GuardError):
            _guard._safe_base(base)

    @pytest.mark.parametrize(("raw", "expected"), [(42, 42), ("0042", 42)])
    def test_safe_pr_normalises_digits(self, raw: int | str, expected: int) -> None:
        assert _guard._safe_pr(raw) == expected

    @pytest.mark.parametrize("raw", ["-1", "abc", "42; rm -rf /", ""])
    def test_safe_pr_rejects_non_digits(self, raw: str) -> None:
        with pytest.raises(_guard.GuardError):
            _guard._safe_pr(raw)

    def test_invalid_repo_fails_the_guard_loudly(self) -> None:
        exit_code = _guard.main(
            [
                "--repo",
                "owner/repo; rm -rf /",
                "--number",
                "2029",
                "--title",
                "feat(FAR-1883): leftovers",
                "--base",
                "main",
            ]
        )

        assert exit_code == _guard.EXIT_GUARD_BROKEN

    def test_post_comment_passes_the_body_via_a_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_run_gh(args, *, expect_failure=False):
            captured["args"] = list(args)
            body_path = args[args.index("--body-file") + 1]
            captured["body"] = Path(body_path).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_guard, "_run_gh", fake_run_gh)

        assert _guard.post_comment("farnalabs/modulo", 2029, "free-form body with --flags")

        assert captured["args"][:3] == ["pr", "comment", "2029"]
        assert "--body" not in captured["args"]
        assert captured["body"] == "free-form body with --flags"
