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

If the diff base cannot be resolved at all (e.g. a shallow clone with no merge
base), the gate does NOT skip: it warns loudly and runs the check
unconditionally.  Running an extra check is safe; silently disabling a gate is
not.
"""

from __future__ import annotations

import re
import subprocess
import sys

_FETCH_TIMEOUT_SECONDS = 30


def _fetch_origin_main() -> None:
    """Best-effort ``git fetch origin main`` so a base ref can (re)appear.

    Called after the first diff attempt fails — a shallow clone may simply be
    missing the merge base.  Quiet on success; never raises (the caller is
    already on its warning path and falls back to running the check
    unconditionally).
    """
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            capture_output=True,
            text=True,
            check=True,
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"warning: best-effort `git fetch origin main` failed: {exc}", file=sys.stderr)


def _git_diff_names(base: str = "origin/main", *, _retry: bool = True) -> list[str] | None:
    """Return files changed between *base* and HEAD.

    Returns ``None`` (not an empty list) when git cannot compute the diff —
    e.g. a missing or stale ``origin/main`` ref — so the caller can warn
    distinctly instead of treating a broken ref as "no files changed".

    On the first failure a single best-effort ``git fetch origin main`` is
    attempted (bounded timeout) and the diff retried once; that path is quiet
    when it succeeds.
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
        if _retry:
            _fetch_origin_main()
            return _git_diff_names(base, _retry=False)
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
        # FAR-1353: the base could not be resolved (even after a best-effort
        # fetch retry).  Never silently skip a gate — run the check anyway and
        # say so loudly.  An extra check run is safe; a gate that looks like
        # "ran and passed" while never executing is not.
        print(
            "warning: cannot resolve diff base against origin/main — running check UNCONDITIONALLY "
            "(base unresolved; skipping would silently disable this gate)",
            file=sys.stderr,
        )
    elif not changed:
        print("no files changed vs origin/main — skipping check", file=sys.stderr)
        return 0
    else:
        matched = _matches_pattern(changed, pattern)
        if not matched:
            print(f"no files matching {pattern} changed — skipping check", file=sys.stderr)
            return 0

        print(f"changed files matching {pattern}: {len(matched)} (running check)", file=sys.stderr)

    # Run the actual command. `cmd` is the literal command declared after `--`
    # in the hook's .pre-commit-config.yaml entry — developer-controlled, not
    # remote/LLM input — and subprocess runs without a shell, so no command
    # injection is reachable (pythonsecurity:S8705 false positive).
    result = subprocess.run(cmd, check=False)  # NOSONAR
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
