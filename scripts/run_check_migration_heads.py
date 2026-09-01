#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the Alembic migration collision check.

Replaces `powershell -NoProfile -File tools/check-migration-heads.ps1`.

Two Workers developing in parallel worktrees can independently create a
migration with the same sequential number (e.g. both add "0062_*.py"), which
doesn't show up as a git merge conflict (different filenames) but corrupts the
migration chain — two files claiming the same numeric slot, or two files with
the same `down_revision`, creating a branch in what should be a single linear
history.

This repo's history already has pre-existing pairs of colliding migration
numbers/down_revisions from before this check existed, so it ONLY evaluates
files that are staged/changed in the current commit — it flags a collision only
when a file being committed right now shares a number, revision, or
down_revision with another file (new or old). Pre-existing collisions between
two files neither of which is part of this commit are left alone.

Intentional forks are exempt: a migration whose own `revision` id appears as a
parent in any merge migration's tuple-form `down_revision` is an intentional
branch, not a collision — those files are excluded from the duplicate-number,
duplicate-revision, and duplicate-down_revision checks.

Also runs `alembic heads` and prints a non-fatal WARNING if more than one head
exists, purely for visibility.

Supports ``--diff-range`` (e.g. "main...HEAD"); when given, the set of
"changed" migration files is taken from `git diff --name-only <range>` instead
of the staged files. Exit 0 clean, 1 collision.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
VERSIONS_DIR = str(Path(REPO_ROOT, "backend", "src", "modulo", "db", "migrations", "versions"))

_RE_REVISION = re.compile(r"(?m)^revision:\s*str\s*=\s*\"([^\"]+)\"")
_RE_DOWN_STRING = re.compile(r"(?m)^down_revision:.*=\s*\"([^\"]+)\"")
_RE_PREFIX = re.compile(r"^(\d{4})_")
_DIFF_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/\-]+$")


def _resolve_repo_root() -> str:
    # Prefer the git repo containing the current directory (so this works from
    # worktree branches, where the branch's new migration files live); fall
    # back to the repo root (tools/ is one level below the repo root).
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        cwd_top = result.stdout.strip()
        if cwd_top and Path(cwd_top, "backend", "src", "modulo", "db", "migrations", "versions").is_dir():
            return cwd_top
    return REPO_ROOT


def _changed_names(diff_range: str | None) -> list[str]:
    if diff_range:
        if not _DIFF_RANGE_RE.match(diff_range):
            print(f"check-migration-heads: invalid --diff-range {diff_range!r}", file=sys.stderr)
            sys.exit(1)
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", diff_range],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            check=False,
        )
    prefix = "backend/src/modulo/db/migrations/versions/"
    names = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith(prefix) and line.endswith(".py"):
            names.append(Path(line).name)
    return names


def _read(path: str) -> str:
    try:
        with Path(path).open(encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _collect_merge_parent_revisions(files: list[str]) -> tuple[set[str], dict[str, str]]:
    """Return (merge_parent_revisions, file_revisions)."""
    merge_parent_revisions: set[str] = set()
    file_revisions: dict[str, str] = {}
    for name in files:
        content = _read(str(Path(VERSIONS_DIR) / name))
        m = _RE_REVISION.search(content)
        if m:
            file_revisions[name] = m.group(1)
        # Tuple-form down_revision (merge migration): the parent list opens with
        # '(' on the down_revision line. Slice from that line through the closing
        # ')' and collect every quoted revision id inside.
        if re.search(r"(?m)^down_revision\s*[:=].*\(", content):
            lines = content.splitlines()
            start = None
            for i, line in enumerate(lines):
                if re.match(r"^down_revision\s*[:=]", line):
                    start = i
                    break
            if start is not None:
                block = ""
                for i in range(start, len(lines)):
                    block += lines[i] + "\n"
                    if ")" in lines[i]:
                        break
                for quoted in re.findall(r"\"([^\"]+)\"", block):
                    merge_parent_revisions.add(quoted)
    return merge_parent_revisions, file_revisions


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Alembic migration number/revision collisions.")
    parser.add_argument(
        "--diff-range",
        default=None,
        help="Optional git diff range (e.g. main...HEAD) for the changed-file set.",
    )
    args = parser.parse_args(argv)

    repo_root = _resolve_repo_root()
    versions_dir = str(Path(repo_root, "backend", "src", "modulo", "db", "migrations", "versions"))

    if not Path(versions_dir).is_dir():
        print(f"check-migration-heads: versions dir not found at {versions_dir} - skipping", file=sys.stderr)
        return 0

    files = sorted(p.name for p in Path(versions_dir).iterdir() if p.name.endswith(".py") and p.name != "__init__.py")

    changed_names = _changed_names(args.diff_range)
    if not changed_names:
        if args.diff_range:
            print(
                f"check-migration-heads: no migration files changed in '{args.diff_range}' - OK",
                file=sys.stderr,
            )
            return 0
        # Nothing staged under versions/ (e.g. running standalone) — fall back to
        # checking every file so the script is still useful ad hoc.
        changed_names = files

    failed = False

    # ---- 0. Merge-parent revisions (intentional forks) ----
    merge_parent_revisions, file_revisions = _collect_merge_parent_revisions(files)

    def non_exempt(names: list[str]) -> list[str]:
        return [n for n in names if not (n in file_revisions and file_revisions[n] in merge_parent_revisions)]

    # ---- 1. Duplicate numeric prefixes ----
    by_prefix: dict[str, list[str]] = {}
    for name in files:
        m = _RE_PREFIX.match(name)
        if m:
            by_prefix.setdefault(m.group(1), []).append(name)
    for prefix, raw_names in sorted(by_prefix.items()):
        names = non_exempt(raw_names)
        involves_changed = any(n in changed_names for n in names)
        if len(names) > 1 and involves_changed:
            print(f"FAIL: duplicate migration number '{prefix}' used by:", file=sys.stderr)
            for name in names:
                print(f"  - {name}", file=sys.stderr)
            print(
                "  > Renumber the one you're adding to the next free sequential number and fix its down_revision.",
                file=sys.stderr,
            )
            failed = True

    # ---- 2. Duplicate revision / down_revision strings ----
    revisions: dict[str, list[str]] = {}
    down_revisions: dict[str, list[str]] = {}
    for name in files:
        content = _read(str(Path(versions_dir) / name))
        m = _RE_REVISION.search(content)
        if m:
            revisions.setdefault(m.group(1), []).append(name)
        d = _RE_DOWN_STRING.search(content)
        if d:
            down_revisions.setdefault(d.group(1), []).append(name)

    for rev, raw_names in sorted(revisions.items()):
        names = non_exempt(raw_names)
        involves_changed = any(n in changed_names for n in names)
        if len(names) > 1 and involves_changed:
            print(f"FAIL: duplicate revision id '{rev}' declared in:", file=sys.stderr)
            for name in names:
                print(f"  - {name}", file=sys.stderr)
            failed = True

    for down, raw_names in sorted(down_revisions.items()):
        names = non_exempt(raw_names)
        involves_changed = any(n in changed_names for n in names)
        if len(names) > 1 and involves_changed:
            print(
                f"FAIL: two migrations both declare down_revision '{down}' - this is an unintended branch:",
                file=sys.stderr,
            )
            for name in names:
                print(f"  - {name}", file=sys.stderr)
            print(
                "  > The one you're adding needs to be rebased on top of the other (renumber + fix down_revision).",
                file=sys.stderr,
            )
            failed = True

    if failed:
        print(
            "\ncheck-migration-heads: FAILED - resolve the migration collisions above before committing.",
            file=sys.stderr,
        )
        return 1

    # ---- 3. Non-fatal: multiple alembic heads ----
    backend_dir = str(Path(repo_root, "backend"))
    try:
        result = subprocess.run(
            ["uv", "run", "python", "-m", "alembic", "heads"],
            cwd=backend_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        head_count = sum(1 for line in result.stdout.splitlines() if "(head)" in line or "(effective head)" in line)
        if head_count > 1:
            print(
                f"WARNING: alembic reports {head_count} migration heads (expected 1):",
                file=sys.stderr,
            )
            for line in result.stdout.splitlines():
                print(f"  {line}", file=sys.stderr)
            print(
                "  This is a non-fatal warning - existing multi-head history is tracked separately.",
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"check-migration-heads: could not run 'alembic heads' to check for multiple heads (non-fatal): {exc}",
            file=sys.stderr,
        )

    print("check-migration-heads: OK - no migration number/revision collisions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
