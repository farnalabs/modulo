#!/usr/bin/env python3
"""Generate a label-driven changelog section from merged pull requests.

Used by the release workflow (``.github/workflows/release.yml``, the
``release`` action) to build the ``CHANGELOG.md`` section and the GitHub
Release notes for a GA tag. Classification is driven entirely by
``changelog:*`` labels:

* ``changelog: breaking``  -> **Breaking Changes**
* ``changelog: feature``   -> **Features**
* ``changelog: fix``       -> **Fixes**
* ``changelog: none``      -> excluded from the listing
* no ``changelog:*`` label -> counted in the trailing "Other" line only

Sections render in the fixed order Breaking Changes, Features, Fixes.
PRs that are excluded or unlabelled are never listed individually; they are
summarised in a single italic "Other" line.

Data collection walks ``git log <from>..<to>`` subjects for PR numbers
(``(#1234)`` suffix and ``Merge pull request #N`` subjects), falling back to
``gh pr list --state merged`` when the range yields none. Per-PR metadata
(labels/title/url) comes from ``gh pr view``. When ``gh`` is unavailable or a
lookup fails the PR is included as unlabelled and a warning is printed to
stderr — a notes failure must never crash the release job.

Dependency-free (stdlib only; ``subprocess`` drives ``git`` / ``gh``).

Unit tests: backend/tests/unit/tools/test_generate_changelog.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

# Label vocabulary (these four labels already exist in the repo).
LABEL_BREAKING = "changelog: breaking"
LABEL_FEATURE = "changelog: feature"
LABEL_FIX = "changelog: fix"
LABEL_NONE = "changelog: none"

# Rendered section headings, in render order.
SECTION_BREAKING = "Breaking Changes"
SECTION_FEATURES = "Features"
SECTION_FIXES = "Fixes"
SECTION_ORDER: tuple[str, ...] = (SECTION_BREAKING, SECTION_FEATURES, SECTION_FIXES)

_LABEL_TO_SECTION: dict[str, str] = {
    LABEL_BREAKING: SECTION_BREAKING,
    LABEL_FEATURE: SECTION_FEATURES,
    LABEL_FIX: SECTION_FIXES,
}

# PR-number extraction from commit subjects: squash-merge suffix "(#1234)"
# at end of subject, and GitHub's own "Merge pull request #1234 from ..." subject.
_PR_SUFFIX_RE = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_SUBJECT_RE = re.compile(r"\bMerge pull request #(\d+)\b")

_UNRELEASED_HEADING_RE = re.compile(r"^## \[Unreleased\]")
_SECTION_HEADING_PREFIX = "## ["


@dataclass(frozen=True)
class PrInfo:
    """Metadata for one merged pull request."""

    number: int
    title: str
    url: str
    labels: tuple[str, ...]


def _warn(msg: str) -> None:
    print(f"generate_changelog: {msg}", file=sys.stderr)


def _run(cmd: Sequence[str]) -> tuple[int, str, str]:
    """Run a subprocess, returning (returncode, stdout, stderr).

    A missing executable degrades to returncode 127 instead of raising, so
    callers can fall back gracefully when ``git``/``gh`` is unavailable.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ---------------------------------------------------------------------------
# Pure helpers (no git/gh — unit-tested directly)
# ---------------------------------------------------------------------------


def extract_pr_numbers(subjects: Sequence[str]) -> list[int]:
    """Extract PR numbers from commit subjects, preserving order, deduped."""
    seen: set[int] = set()
    numbers: list[int] = []
    for subject in subjects:
        match = _PR_SUFFIX_RE.search(subject) or _MERGE_SUBJECT_RE.search(subject)
        if match is None:
            continue
        number = int(match.group(1))
        if number in seen:
            continue
        seen.add(number)
        numbers.append(number)
    return numbers


def classify_prs(prs: Sequence[PrInfo]) -> tuple[dict[str, list[PrInfo]], int]:
    """Group PRs into changelog sections and count the 'Other' remainder.

    Returns ``(groups, other_count)`` where *groups* has exactly the
    ``SECTION_ORDER`` keys (possibly empty lists) and *other_count* is the
    number of PRs with ``changelog: none`` or no ``changelog:*`` label —
    i.e. every PR NOT listed in any section. Section labels win over
    ``changelog: none`` if somehow both are applied; between section labels
    the ``SECTION_ORDER`` precedence applies (breaking first).
    """
    groups: dict[str, list[PrInfo]] = {name: [] for name in SECTION_ORDER}
    other_count = 0
    for pr in prs:
        label_set = set(pr.labels)
        placed = False
        for section_name in SECTION_ORDER:
            if any(_LABEL_TO_SECTION.get(label) == section_name for label in label_set):
                groups[section_name].append(pr)
                placed = True
                break
        if not placed:
            # Either explicitly 'changelog: none' or unlabelled — both are
            # counted in the Other line and never listed individually.
            other_count += 1
    return groups, other_count


def render_release_section(
    version: str,
    groups: dict[str, list[PrInfo]],
    other_count: int,
    summary: str,
    when: date,
) -> str:
    """Render the ``## [version]`` markdown section.

    The optional *summary* (hand-written narrative) sits directly below the
    version heading, above the sections. Empty sections are omitted. The
    'Other' line is emitted only when at least one PR falls outside the
    listed sections.
    """
    lines: list[str] = [f"## [{version}] — {when.isoformat()}", ""]
    stripped_summary = summary.strip()
    if stripped_summary:
        lines.extend([stripped_summary, ""])
    for section_name in SECTION_ORDER:
        entries = groups.get(section_name, [])
        if not entries:
            continue
        lines.append(f"### {section_name}")
        for pr in entries:
            if pr.url:
                lines.append(f"- [#{pr.number}]({pr.url}) — {pr.title}")
            else:
                lines.append(f"- #{pr.number} — {pr.title}")
        lines.append("")
    if other_count > 0:
        lines.append(f"_Plus {other_count} internal changes and fixes._")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def upsert_section(doc: str, section: str, version: str) -> str:
    """Insert *section* into *doc*, or replace an existing ``## [version]``.

    Insertion point: directly below the file header and any leading
    ``## [Unreleased]`` section, so the newest release sits at the top of the
    released history. All pre-existing content outside the replaced/inserted
    section is left intact. Running twice with the same *section* is a no-op
    (idempotent — never duplicates).
    """
    lines = doc.splitlines()
    heading_re = re.compile(rf"^{re.escape(_SECTION_HEADING_PREFIX)}{re.escape(version)}\]")

    body = section.splitlines()
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()

    # Replace an existing section for this version, if present.
    for idx, line in enumerate(lines):
        if heading_re.match(line):
            end = next(
                (i for i in range(idx + 1, len(lines)) if lines[i].startswith(_SECTION_HEADING_PREFIX)),
                len(lines),
            )
            return _concat(lines[:idx], body, lines[end:])

    # Insertion point: after the header, skipping any leading Unreleased section.
    starts = [i for i, line in enumerate(lines) if line.startswith(_SECTION_HEADING_PREFIX)]
    if starts:
        insert_at = starts[0]
        while insert_at < len(lines) and _UNRELEASED_HEADING_RE.match(lines[insert_at]):
            insert_at = next(
                (i for i in range(insert_at + 1, len(lines)) if lines[i].startswith(_SECTION_HEADING_PREFIX)),
                len(lines),
            )
    else:
        insert_at = len(lines)
    return _concat(lines[:insert_at], body, lines[insert_at:])


def _concat(*chunks: list[str]) -> str:
    """Join line chunks, inserting a single blank line between non-blank
    boundaries and normalising to exactly one trailing newline."""
    out: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if out and out[-1].strip() and chunk[0].strip():
            out.append("")
        out.extend(chunk)
    while out and not out[-1].strip():
        out.pop()
    if not out:
        return ""
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# git / gh collection (graceful degradation — never fatal)
# ---------------------------------------------------------------------------


def _git_log_subjects(from_ref: str, to_ref: str) -> list[str]:
    rc, out, err = _run(["git", "log", f"{from_ref}..{to_ref}", "--pretty=%s"])
    if rc != 0:
        _warn(f"git log {from_ref}..{to_ref} failed ({err.strip() or f'exit {rc}'}); falling back to gh")
        return []
    return [line for line in out.splitlines() if line.strip()]


def _gh_available() -> bool:
    rc, _, _ = _run(["gh", "--version"])
    return rc == 0


def _gh_merged_pr_numbers(limit: int) -> list[int]:
    rc, out, err = _run(["gh", "pr", "list", "--state", "merged", "--limit", str(limit), "--json", "number"])
    if rc != 0:
        _warn(f"gh pr list failed ({err.strip() or f'exit {rc}'}); changelog will be empty")
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        _warn("gh pr list returned non-JSON output; changelog will be empty")
        return []
    if not isinstance(data, list):
        _warn("gh pr list returned unexpected JSON shape; changelog will be empty")
        return []
    return [item["number"] for item in data if isinstance(item, dict) and isinstance(item.get("number"), int)]


def _unlabelled_pr(number: int) -> PrInfo:
    return PrInfo(number=number, title=f"#{number}", url="", labels=())


def _fetch_pr_info(number: int) -> PrInfo:
    """Fetch labels/title/url for one PR; degrade to unlabelled on any failure."""
    rc, out, err = _run(["gh", "pr", "view", str(number), "--json", "labels,title,url"])
    if rc != 0:
        _warn(f"could not fetch PR #{number} ({err.strip() or f'exit {rc}'}); including it as unlabelled")
        return _unlabelled_pr(number)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        _warn(f"gh pr view #{number} returned non-JSON; including it as unlabelled")
        return _unlabelled_pr(number)
    if not isinstance(data, dict):
        _warn(f"gh pr view #{number} returned unexpected JSON shape; including it as unlabelled")
        return _unlabelled_pr(number)
    raw_labels = data.get("labels")
    labels: list[str] = []
    if isinstance(raw_labels, list):
        labels.extend(
            entry["name"] for entry in raw_labels if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        )
    raw_title = data.get("title")
    title = raw_title if isinstance(raw_title, str) else f"#{number}"
    raw_url = data.get("url")
    url = raw_url if isinstance(raw_url, str) else ""
    return PrInfo(number=number, title=title, url=url, labels=tuple(labels))


def collect_prs(from_ref: str, to_ref: str, pr_limit: int) -> list[PrInfo]:
    """Collect PR metadata for the range, degrading gracefully throughout."""
    subjects = _git_log_subjects(from_ref, to_ref)
    numbers = extract_pr_numbers(subjects)
    if not numbers:
        _warn(
            f"no PR numbers found in git subjects for {from_ref}..{to_ref}; falling back to 'gh pr list --state merged'"
        )
        numbers = _gh_merged_pr_numbers(pr_limit)
    numbers = numbers[:pr_limit]
    if not numbers:
        return []
    if not _gh_available():
        _warn("gh CLI unavailable; including all PRs as unlabelled")
        return [_unlabelled_pr(number) for number in numbers]
    return [_fetch_pr_info(number) for number in numbers]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a label-driven changelog section from merged PRs.",
    )
    parser.add_argument("--from", dest="from_ref", required=True, metavar="<ref>", help="start ref (exclusive)")
    parser.add_argument("--to", dest="to_ref", default="HEAD", metavar="<ref>", help="end ref (inclusive)")
    parser.add_argument("--version", required=True, metavar="<X.Y.Z>", help="release version being written")
    parser.add_argument("--output", type=Path, metavar="<path>", help="CHANGELOG.md to prepend/replace into")
    parser.add_argument("--summary", default="", metavar="<prose>", help="high-level narrative for the version section")
    parser.add_argument("--body-only", action="store_true", help="print markdown only; do not touch any file")
    parser.add_argument(
        "--pr-limit",
        type=int,
        default=200,
        metavar="<n>",
        help="maximum PRs to inspect (also the gh fallback limit)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    prs = collect_prs(args.from_ref, args.to_ref, args.pr_limit)
    groups, other_count = classify_prs(prs)
    section = render_release_section(args.version, groups, other_count, args.summary, datetime.now(UTC).date())
    sys.stdout.write(section)
    if args.body_only or args.output is None:
        return 0
    existing = ""
    if args.output.exists():
        existing = args.output.read_text(encoding="utf-8")
    updated = upsert_section(existing, section, args.version)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
