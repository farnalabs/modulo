#!/usr/bin/env python3
"""Gate a pre-push check: only run the actual command when relevant files changed.

Used by the pre-push hooks (mypy, schema-freshness) to skip expensive checks
when no relevant source files changed between the branch and origin/main.

Usage:
    python scripts/run_prepush_gated_check.py --pattern "backend/src/**/*.py" -- uv --directory backend run mypy src/modulo/
    python scripts/run_prepush_gated_check.py --pattern "backend/src/**/*.py" -- pnpm run type-check

The --pattern glob is matched against files changed between origin/main and HEAD.
If zero files match, the check is skipped (exit 0).  If files match, the command
after ``--`` is executed.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path


def _git_diff_names(base: str = "origin/main") -> list[str]:
    """Return files changed between base and HEAD."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [l for l in result.stdout.splitlines() if l]
    except subprocess.CalledProcessError:
        return []


def _matches_pattern(files: list[str], pattern: str) -> list[str]:
    """Return files matching the glob pattern (/**/ matches any depth)."""
    matched = []
    for f in files:
        # Convert repo-relative path to POSIX for matching
        posix = f.replace("\\", "/")
        if fnmatch.fnmatch(posix, pattern) or fnmatch.fnmatch(posix, pattern.rstrip("/*")):
            matched.append(f)
    return matched


def main() -> int:
    args = sys.argv[1:]

    # Parse --pattern <glob>
    pattern = None
    cmd_start = 0
    i = 0
    while i < len(args):
        if args[i] == "--pattern" and i + 1 < len(args):
            pattern = args[i + 1]
            i += 2
        elif args[i] == "--":
            cmd_start = i + 1
            break
        else:
            i += 1

    if pattern is None or cmd_start >= len(args):
        print("Usage: run_prepush_gated_check.py --pattern <glob> -- <command...>", file=sys.stderr)
        return 2

    # Check changed files
    changed = _git_diff_names()
    if not changed:
        print("no files changed vs origin/main — skipping check", file=sys.stderr)
        return 0

    matched = _matches_pattern(changed, pattern)
    if not matched:
        print(f"no files matching {pattern} changed — skipping check", file=sys.stderr)
        return 0

    print(f"changed files matching {pattern}: {len(matched)} (running check)", file=sys.stderr)

    # Run the actual command
    cmd = args[cmd_start:]
    result = subprocess.run(cmd, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
