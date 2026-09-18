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

import re
import subprocess
import sys


def _git_diff_names(base: str = "origin/main") -> list[str] | None:
    """Return files changed between *base* and HEAD.

    Returns ``None`` (not an empty list) when git cannot compute the diff —
    e.g. a missing or stale ``origin/main`` ref — so the caller can warn
    distinctly instead of treating a broken ref as "no files changed".
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        hint = detail[0] if detail else f"exit code {exc.returncode}"
        print(f"warning: `git diff {base}...HEAD` failed: {hint}", file=sys.stderr)
        return None
    return [line for line in result.stdout.splitlines() if line]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a repository-relative glob into a compiled regex.

    ``fnmatch`` gives ``**`` no special meaning and lets a single ``*`` span
    path separators, so ``backend/src/**/*.py`` requires a literal extra ``/``
    after ``backend/src/`` and does NOT match ``backend/src/foo.py`` (the gate
    then silently skips).  This translator implements gitignore-style
    semantics instead:

    - ``**/`` matches zero or more directories,
    - ``**``  matches anything, including ``/``,
    - ``*``   matches anything except ``/``,
    - ``?``   matches a single character other than ``/``.

    The pattern is anchored, so it must match the whole path.
    """
    out = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _matches_pattern(files: list[str], pattern: str) -> list[str]:
    """Return files matching the glob *pattern* (gitignore-style ``**``)."""
    regex = _glob_to_regex(pattern)
    # Normalise Windows separators so a checkout on Windows matches too.
    return [f for f in files if regex.match(f.replace("\\", "/"))]


def main() -> int:
    args = sys.argv[1:]

    # Parse --pattern <glob> ... -- <command...>
    pattern = None
    cmd: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--pattern" and i + 1 < len(args):
            pattern = args[i + 1]
            i += 2
        elif args[i] == "--":
            cmd = args[i + 1 :]
            break
        else:
            print(f"Usage: unexpected argument {args[i]!r}", file=sys.stderr)
            return 2

    if pattern is None or not cmd:
        print("Usage: run_prepush_gated_check.py --pattern <glob> -- <command...>", file=sys.stderr)
        return 2

    # Check changed files
    changed = _git_diff_names()
    if changed is None:
        print("warning: cannot diff against origin/main — skipping check (CI still runs it)", file=sys.stderr)
        return 0
    if not changed:
        print("no files changed vs origin/main — skipping check", file=sys.stderr)
        return 0

    matched = _matches_pattern(changed, pattern)
    if not matched:
        print(f"no files matching {pattern} changed — skipping check", file=sys.stderr)
        return 0

    print(f"changed files matching {pattern}: {len(matched)} (running check)", file=sys.stderr)

    # Run the actual command
    result = subprocess.run(cmd, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
