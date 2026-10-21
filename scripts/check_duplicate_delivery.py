#!/usr/bin/env python3
"""Flag two OPEN pull requests that claim the same Linear ticket.

Runs at ``pull_request`` (opened / edited / reopened). The incident this guards
is recorded in ``docs/operations/far-1883-duplicate-delivery.md``: PR #2017 and
PR #2029 both carried ``FAR-1883`` in their titles, #2029's branch was cut from
#2017's head so its diff re-carried the whole feature, and when ``main``
independently took the shared migration's numeric prefix the merge queue saw
two Alembic heads and aborted an entire batch of ten approved PRs for ~4 hours.

ADVISORY BY DESIGN - a collision never exits non-zero. The cost of that
incident was ONE pull request wedging everything, so a guard with a
false-positive path must not be able to reproduce it. A collision prints a loud
``::warning::``, posts a durable comment naming both PRs, and adds the
``duplicate-delivery`` label. Exit code 2 is reserved for the guard being
unable to do its job at all (it could not list the repo's open PRs): a guard
that cannot see its input must fail loudly, not report a silent green.

Detection rules (pure, and unit-tested in
``backend/tests/architecture/test_duplicate_delivery_guard.py``):

* The ticket is the FIRST ``FAR-<digits>`` token in the title, matched
  case-insensitively (``far-1883`` is the same ticket as ``FAR-1883``).
* No ticket in the title -> nothing to compare, exit 0 with no output.
* A collision is another OPEN pull request against the SAME base branch whose
  title names the same ticket.
* ``[partial]`` exempts BOTH sides. The repo convention is that every PR in a
  chained sweep except the final one carries ``[partial]`` in its title, so two
  ``[partial]`` PRs for one ticket are a legal chain, not a duplicate.
* A PR is never compared against itself.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: The label added to a PR that collides with another open PR.
LABEL = "duplicate-delivery"
LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "Another open PR claims the same Linear ticket"

#: First FAR-<digits> in a title. ``\b`` on both sides so ``XFAR-1`` is not a
#: ticket and ``FAR-1883-extra`` still reads as ``FAR-1883``.
TICKET_RE = re.compile(r"\bFAR-(\d+)\b", re.IGNORECASE)

#: The chained-sweep marker. Every PR in a chain except the final one carries it.
PARTIAL_RE = re.compile(r"\[partial\]", re.IGNORECASE)

#: Hidden, machine-stable idempotency key. One comment per counterpart PR: a
#: re-open / re-edit of the triggering PR must not re-post it.
MARKER_TEMPLATE = "<!-- duplicate-delivery-guard:counterpart={number} -->"

EXIT_OK = 0
EXIT_GUARD_BROKEN = 2


class GuardError(RuntimeError):
    """The guard could not read the state it needs to compare."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str


def extract_ticket(title: str) -> str | None:
    """The ticket a PR title claims, normalised to ``FAR-<n>``, or ``None``."""
    match = TICKET_RE.search(title)
    if match is None:
        return None
    return f"FAR-{int(match.group(1))}"


def is_partial(title: str) -> bool:
    """Whether the title carries the chained-sweep ``[partial]`` marker."""
    return PARTIAL_RE.search(title) is not None


def find_duplicate_deliveries(subject: PullRequest, others: Iterable[PullRequest]) -> list[PullRequest]:
    """Open PRs that claim the same ticket as ``subject``, lowest number first.

    ``others`` is expected to be the open PRs against the subject's base branch;
    the base comparison itself is the caller's job (``gh pr list --base``), so
    this stays a pure function over titles and numbers.
    """
    ticket = extract_ticket(subject.title)
    if ticket is None:
        return []
    if is_partial(subject.title):
        return []
    duplicates = [
        other
        for other in others
        if other.number != subject.number and extract_ticket(other.title) == ticket and not is_partial(other.title)
    ]
    return sorted(duplicates, key=lambda pr: pr.number)


def comment_marker(counterpart_number: int) -> str:
    """The hidden idempotency key for the comment about one counterpart PR."""
    return MARKER_TEMPLATE.format(number=counterpart_number)


def already_reported(existing_bodies: Iterable[str], counterpart_number: int) -> bool:
    """Whether a comment about this counterpart is already on the PR."""
    marker = comment_marker(counterpart_number)
    return any(marker in body for body in existing_bodies)


def pull_request_link(number: int, title: str) -> str:
    """``#<n> ``(``<title>``)`` with the title quoted so a bracket is literal."""
    return f"#{number} (`{title}`)"


def render_comment(subject: PullRequest, counterpart: PullRequest, ticket: str) -> str:
    """The durable PR comment for one collision."""
    return "\n".join(
        [
            comment_marker(counterpart.number),
            "## Possible duplicate delivery",
            "",
            (
                f"This PR {pull_request_link(subject.number, subject.title)} and "
                f"{pull_request_link(counterpart.number, counterpart.title)} are both **open** "
                f"and both name **{ticket}** in their title."
            ),
            "",
            "Two open PRs for one ticket is usually one of these:",
            "",
            "- **A stacked PR.** The other branch is cut from this PR head (or vice versa), so its diff",
            "  re-carries the whole feature. Retarget it at `main` once the base PR merges, or close it.",
            "- **A chained sweep slice.** Every PR in a chain except the final one carries `[partial]` in",
            "  its title. Without that marker a chain and a duplicate look identical.",
            "- **A genuine duplicate.** One PR should be closed and its ticket kept against the survivor.",
            "",
            "**This is a warning, not a block.** No check is failing because of it and the merge queue is",
            "not held. Confirm which PR is the intended delivery before merging: when two branches carry",
            "the same Alembic migration, the second one's numeric prefix can be taken by `main` and abort",
            "a whole merge-queue batch (2026-10-20, FAR-1883).",
            "",
            "_Posted once per colliding PR by the `duplicate-delivery-guard` workflow._",
        ]
    )


#: Taint barriers for operator-supplied argv values (pythonsecurity:S8705).
#:
#: ``--repo``, ``--number`` and ``--base`` come from a workflow event and are
#: forwarded into ``gh`` invocations. Each is bounded to its legitimate
#: character set with ``re.fullmatch`` and only the fresh match result is
#: passed on, so a crafted value can never be read by ``gh`` as a CLI flag.
#: Mirrors the established pattern in ``scripts/fast_lane_classify.py``.
#: Comment bodies never travel through argv at all: they are written to a
#: temporary file and passed with ``gh pr comment --body-file``.
_REPO_ARG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_BASE_ARG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_PR_ARG_RE = re.compile(r"\d+")


def _safe_arg(value: str, pattern: re.Pattern[str]) -> str | None:
    """Return *value* only when it fully matches *pattern*."""
    matched = pattern.fullmatch(value or "")
    return matched.group(0) if matched else None


def _safe_repo(repo: str) -> str:
    """Bound a ``owner/repo`` slug before it reaches a subprocess argv."""
    validated = _safe_arg(repo, _REPO_ARG_RE)
    if validated is None:
        raise GuardError(f"invalid repository slug {repo!r}")
    return validated


def _safe_base(base: str) -> str:
    """Bound a base branch name before it reaches a subprocess argv."""
    validated = _safe_arg(base, _BASE_ARG_RE)
    if validated is None:
        raise GuardError(f"invalid base branch {base!r}")
    return validated


def _safe_pr(number: int | str) -> int:
    """Bound a PR number to digits and return it normalised to ``int``."""
    validated = _safe_arg(str(number), _PR_ARG_RE)
    if validated is None:
        raise GuardError(f"invalid PR number {number!r}")
    return int(validated)


def _run_gh(args: Sequence[str], *, expect_failure: bool = False) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` and return the completed process (never raises on its own)."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and not expect_failure:
        raise GuardError(f"`gh {' '.join(args[:2])}` failed (exit {result.returncode}): {result.stderr.strip()}")
    return result


def list_open_prs(repo: str, base: str) -> list[PullRequest]:
    """Every OPEN pull request targeting ``base`` in ``repo``."""
    result = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--base",
            base,
            "--limit",
            "200",
            "--json",
            "number,title",
        ]
    )
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive, gh emits JSON
        raise GuardError(f"could not parse the open-PR list: {exc}") from exc
    return [PullRequest(number=int(item["number"]), title=str(item["title"])) for item in payload]


def list_comment_bodies(repo: str, number: int) -> list[str]:
    """Every comment body currently on a PR."""
    result = _run_gh(["pr", "view", str(number), "--repo", repo, "--json", "comments"])
    payload = json.loads(result.stdout or "{}")
    return [str(comment.get("body", "")) for comment in payload.get("comments", [])]


def post_comment(repo: str, number: int, body: str) -> bool:
    """Post a PR comment. Best effort: returns False instead of raising.

    The body is written to a temporary file and passed with ``--body-file`` so
    free-form PR text never becomes a command-line argument
    (pythonsecurity:S8705).
    """
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    try:
        result = _run_gh(
            ["pr", "comment", str(number), "--repo", repo, "--body-file", body_path],
            expect_failure=True,
        )
    finally:
        Path(body_path).unlink(missing_ok=True)
    return result.returncode == 0


def ensure_label(repo: str) -> bool:
    """Create the label if it does not exist. Best effort: False on failure."""
    exists = _run_gh(["api", f"repos/{repo}/labels/{LABEL}"], expect_failure=True)
    if exists.returncode == 0:
        return True
    created = _run_gh(
        [
            "api",
            "-X",
            "POST",
            f"repos/{repo}/labels",
            "-f",
            f"name={LABEL}",
            "-f",
            f"color={LABEL_COLOR}",
            "-f",
            f"description={LABEL_DESCRIPTION}",
        ],
        expect_failure=True,
    )
    return created.returncode == 0


def add_label(repo: str, number: int) -> bool:
    """Add the label to a PR through the REST API. Best effort: False on failure.

    Deliberately NOT ``gh pr edit --add-label``: that command exits 0 while
    applying nothing (FAR-1173), which would make this guard silently do
    nothing.
    """
    result = _run_gh(
        ["api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={LABEL}"],
        expect_failure=True,
    )
    return result.returncode == 0


def escape_annotation(text: str) -> str:
    """Escape text for a GitHub Actions ``::warning::`` annotation message.

    A PR title can contain ``%`` ("cut cost 50%"), which the runner would
    otherwise try to decode as a workflow-command escape sequence.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--repo", required=True, help="owner/name of the repository")
    parser.add_argument("--number", required=True, type=int, help="number of the triggering PR")
    parser.add_argument("--title", required=True, help="title of the triggering PR")
    parser.add_argument("--base", required=True, help="base branch the triggering PR targets")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the collisions and comments without posting anything",
    )
    return parser


def _report_collision(
    repo: str,
    subject: PullRequest,
    counterpart: PullRequest,
    ticket: str,
    *,
    dry_run: bool,
    label_ready: bool | None,
) -> bool | None:
    """Warn about one collision and, unless dry-run, comment and label it.

    Returns the cached label state: ``None`` when not yet checked, ``True``
    when the label is available and ``False`` when it is not.
    """
    body = render_comment(subject, counterpart, ticket)
    print(
        f"::warning::PR #{subject.number} and PR #{counterpart.number} both claim {ticket}: "
        f"{escape_annotation(counterpart.title)}"
    )
    if dry_run:
        print(body)
        return label_ready

    try:
        existing = list_comment_bodies(repo, subject.number)
    except GuardError:
        # Cannot prove the comment is absent, so do not risk a duplicate.
        print(f"::warning::could not read existing comments on #{subject.number}; not re-posting")
        return label_ready

    if already_reported(existing, counterpart.number):
        print(f"duplicate-delivery-guard: already reported the collision with #{counterpart.number}; skipping.")
    elif post_comment(repo, subject.number, body):
        print(f"duplicate-delivery-guard: commented on #{subject.number} about #{counterpart.number}.")
    else:
        print(
            f"::warning::could not post the duplicate-delivery comment on #{subject.number} about "
            f"#{counterpart.number} (read-only token, e.g. a fork PR?); the collision is recorded in this log only"
        )

    if label_ready is None:
        label_ready = ensure_label(repo)
    if label_ready and add_label(repo, subject.number):
        print(f"duplicate-delivery-guard: labelled #{subject.number} '{LABEL}'.")
    else:
        print(f"::warning::could not apply the '{LABEL}' label to #{subject.number}; the comment is the durable signal")
    return label_ready


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # S8705: bound operator-supplied argv values before any ``gh`` invocation.
    try:
        repo = _safe_repo(args.repo)
        base = _safe_base(args.base)
        number = _safe_pr(args.number)
    except GuardError as exc:
        print(f"::error::duplicate-delivery-guard received invalid input: {exc}")
        return EXIT_GUARD_BROKEN

    subject = PullRequest(number=number, title=args.title)

    ticket = extract_ticket(subject.title)
    if ticket is None:
        print(f"duplicate-delivery-guard: #{subject.number} names no FAR-<n> ticket; nothing to compare.")
        return EXIT_OK

    if is_partial(subject.title):
        print(f"duplicate-delivery-guard: #{subject.number} is marked [partial]; a chained sweep is not a duplicate.")
        return EXIT_OK

    try:
        others = list_open_prs(repo, base)
    except GuardError as exc:
        print(f"::error::duplicate-delivery-guard could not list open PRs against {base}: {exc}")
        return EXIT_GUARD_BROKEN

    duplicates = find_duplicate_deliveries(subject, others)
    if not duplicates:
        print(f"duplicate-delivery-guard: {ticket} is claimed by no other open PR against {base}.")
        return EXIT_OK

    label_ready: bool | None = None
    for counterpart in duplicates:
        label_ready = _report_collision(
            repo,
            subject,
            counterpart,
            ticket,
            dry_run=args.dry_run,
            label_ready=label_ready,
        )

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
