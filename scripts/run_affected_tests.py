#!/usr/bin/env python3
"""Fast best-guess affected-tests gate for pre-push.

Runs the changed test files plus candidate test files for changed source
modules — NOT the full backend suite.  The full backend unit suite runs in
CI's "Test (Backend)" job.

Usage:
    python scripts/run_affected_tests.py [--list] [--base <ref>]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXCLUDE_NAMES: frozenset[str] = frozenset({"conftest.py", "__init__.py"})
MAX_TEST_FILES = 300


def _git(*args: str) -> str | None:
    """Run a git command and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _resolve_merge_base(base_ref: str) -> str | None:
    """Return the merge-base between *base_ref* and HEAD, or None."""
    sha = _git("merge-base", base_ref, "HEAD")
    return sha


def _collect_changed_files(base: str) -> list[str]:
    """Return the list of files changed between *base* and HEAD."""
    merge_base = _resolve_merge_base(base)
    if merge_base is None:
        # Fallback: HEAD~1
        merge_base = _resolve_merge_base("HEAD~1")
        if merge_base is None:
            print(
                "notice: cannot determine merge-base with origin/main or HEAD~1 — "
                "skipping (CI runs the full suite)",
                file=sys.stderr,
            )
            return []
    raw = _git("diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}...HEAD")
    if not raw:
        return []
    return raw.splitlines()


def _stem(path: str) -> str:
    """Return filename stem (no extension) from a POSIX-style path."""
    return Path(path).stem


def _select_test_files(changed: list[str]) -> list[str]:
    """Derive the set of test file paths (repo-relative) to run."""
    selected: set[str] = set()
    backend_root = Path("backend")

    for f in changed:
        if not f.startswith("backend/"):
            continue
        name = Path(f).name
        if name in EXCLUDE_NAMES:
            continue

        if f.startswith("backend/tests/") and f.endswith(".py"):
            selected.add(f)
            continue

        if f.startswith("backend/src/") and f.endswith(".py"):
            stem = _stem(f)
            pattern = str(backend_root / "tests" / "**" / f"test_{stem}*.py")
            for match in sorted(backend_root.parent.glob(pattern)):
                rel = match.as_posix()
                if Path(rel).name not in EXCLUDE_NAMES:
                    selected.add(rel)

    return sorted(selected)[:MAX_TEST_FILES]


def _run_pytest(test_paths: list[str]) -> int:
    """Run pytest on *test_paths* from the backend/ directory. Returns exit code."""
    cmd = [
        "uv", "run", "--no-sync", "pytest",
        "--tb=short", "-q", "--timeout=120",
        *test_paths,
    ]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    print(f"from:    backend/", file=sys.stderr)
    result = subprocess.run(cmd, cwd="backend")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    list_only = False
    base = "origin/main"

    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--list":
            list_only = True
        elif args[i] == "--base":
            i += 1
            if i >= len(args):
                print("error: --base requires a value", file=sys.stderr)
                return 2
            base = args[i]
        else:
            positional.append(args[i])
        i += 1

    changed = _collect_changed_files(base)
    if not changed:
        print(
            "no affected tests selected - skipping (CI runs the full suite)",
            file=sys.stderr,
        )
        return 0

    test_paths = _select_test_files(changed)
    if not test_paths:
        print(
            "no affected tests selected - skipping (CI runs the full suite)",
            file=sys.stderr,
        )
        return 0

    if list_only:
        for p in test_paths:
            print(p)
        return 0

    print(f"selected {len(test_paths)} test file(s):", file=sys.stderr)
    for p in test_paths:
        print(f"  {p}", file=sys.stderr)

    # Run pytest with paths relative to backend/
    relative_paths = [p.removeprefix("backend/").removeprefix("./") for p in test_paths]
    return _run_pytest(relative_paths)


if __name__ == "__main__":
    sys.exit(main())
