#!/usr/bin/env python3
"""Block a first push ONLY when the branch conflicts with origin/main.

Pre-push gate (``.pre-commit-config.yaml`` hook
``prepush-merge-conflict-check``): a branch being pushed for the first time
(no upstream configured) is checked against ``origin/main`` for merge
conflicts.  If the branch merges cleanly, push as-is — no rebase required.
Conflicts are reported with the offending paths so the developer can resolve
them before pushing.

Behaviour:
- branch already pushed  -> pass (first-push rule only)
- no conflicts           -> pass (merges cleanly)
- conflicts              -> FAIL with conflicted paths
- origin unreachable     -> WARN and pass (never block work on network)
- detached HEAD / unborn -> WARN and pass (cannot reason about branch)

Stdlib-only; cross-platform (Windows + Linux).
"""

from __future__ import annotations

import subprocess
import sys

_PREFIX = "prepush-merge-conflict-check:"


def _run_git(*args: str) -> tuple[int, str, str]:
    # text=False on stderr would mangle messages; keep stdout text (ASCII
    # ref names only) and let stderr come back as-is via capture.
    proc = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _warn(msg: str) -> None:
    print(f"{_PREFIX} {msg}", file=sys.stderr)


def _parse_conflicted_paths(stdout: str) -> list[str]:
    """Return the conflicted file paths from ``git merge-tree`` output.

    The first line is the resulting tree OID; remaining non-empty lines are
    conflicted paths.
    """
    lines = stdout.strip().splitlines()
    if len(lines) < 2:
        return []
    return [ln for ln in lines[1:] if ln.strip()]


def main() -> int:
    # Which remote to compare against.
    rc, out, _ = _run_git("remote")
    origin_available = rc == 0 and "origin" in out.splitlines()

    rc, out, _ = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    branch = out.strip() if rc == 0 else ""
    if rc != 0 or not branch or branch == "HEAD":
        _warn("detached HEAD / unresolved branch - skipping (pass)")
        return 0

    # Has this branch been pushed before (upstream configured)?
    # `@{u}` fails with exit code 128 when no upstream is set.
    # Treat upstream == origin/main as unpushed (worktree-creation flows set
    # the upstream to origin/main even though the branch has never been pushed
    # to its own remote branch).
    rc_u, out_u, _ = _run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream = out_u.strip()
    first_push = rc_u != 0 or not upstream or upstream == "origin/main"
    if not first_push:
        _warn(f"'{branch}' already has upstream '{upstream}' - pass (first-push rule only).")
        return 0

    if not origin_available:
        _warn("no 'origin' remote available - skipping merge-conflict check (pass).")
        return 0

    rc_f, out_f, err_f = _run_git("fetch", "origin")
    if rc_f != 0:
        _warn(
            f"'git fetch origin' failed "
            f"({(err_f or out_f).strip() or 'no output'}); "
            "skipping merge-conflict check (pass)."
        )
        return 0

    rc_v, _, _ = _run_git("rev-parse", "--verify", "--quiet", "origin/main")
    if rc_v != 0:
        _warn("origin/main does not resolve locally - skipping merge-conflict check (pass).")
        return 0

    # If origin/main is already an ancestor of HEAD, nothing can conflict.
    rc_a, _, _ = _run_git("merge-base", "--is-ancestor", "origin/main", "HEAD")
    if rc_a == 0:
        _warn(f"'{branch}' is based on latest origin/main - pass.")
        return 0

    # Test whether the branch merges cleanly with origin/main.
    rc_m, out_m, _err_m = _run_git("merge-tree", "--write-tree", "--name-only", "origin/main", "HEAD")
    if rc_m == 0:
        _warn(f"'{branch}' merges cleanly with origin/main - pass.")
        return 0

    if rc_m == 1:
        paths = _parse_conflicted_paths(out_m)
        if paths:
            path_block = "\n".join(f"  {p}" for p in paths)
        else:
            path_block = "  (conflict detail is in git's output — see stderr above)"
        print(
            f"{_PREFIX} FAILED - '{branch}' conflicts with origin/main.\n"
            f"Conflicted path(s):\n{path_block}\n"
            "Resolve the conflict before pushing, e.g.:\n"
            "  git fetch origin main && git merge origin/main    # or: git rebase origin/main",
            file=sys.stderr,
        )
        return 1

    # Unexpected exit code (old git, unrelated histories, etc.) — fail open.
    _warn(f"git merge-tree exited with unexpected code {rc_m} - skipping merge-conflict check (pass).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
