#!/usr/bin/env python3
"""Enforce rebase-onto-latest-main for a branch's FIRST push.

Pre-push gate (`.pre-commit-config.yaml` hook ``prepush-rebase-check``):
a branch being pushed for the first time (no upstream configured) must be a
descendant of the latest ``origin/main``. After the first push we allow merge
commits and never force a rebase on a shared branch, so branches WITH an
upstream always pass.

Behaviour:
- upstream exists            -> pass (branch is shared/pushed before)
- no upstream + ancestor     -> pass (correctly rebased off latest main)
- no upstream + not ancestor -> FAIL with the exact rebase command to run
- origin unreachable/offline -> WARN and pass (never block work on network)
- detached HEAD / unborn     -> WARN and pass (cannot reason about branch)

stdlib-only; cross-platform (Windows + Linux).
"""

from __future__ import annotations

import subprocess
import sys


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


def main() -> int:
    # Which remote to compare against.
    rc, out, _ = _run_git("remote")
    origin_available = rc == 0 and "origin" in out.splitlines()

    rc, out, _ = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    branch = out.strip() if rc == 0 else ""
    if rc != 0 or not branch or branch == "HEAD":
        print("prepush-rebase-check: detached HEAD / unresolved branch - skipping (pass)", file=sys.stderr)
        return 0

    # Has this branch been pushed before (upstream configured)?
    # `@{u}` fails with exit code 128 when no upstream is set.
    # TREAT UPSTREAM == origin/main AS UNPUSHED: `git checkout -b feat/x
    # origin/main` (and some worktree-creation flows) silently sets the
    # upstream to origin/main, yet the branch has never been pushed to its
    # own remote branch — the first-push rebase rule must still apply there.
    rc_u, out_u, _ = _run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream = out_u.strip()
    first_push = rc_u != 0 or not upstream or upstream == "origin/main"
    if not first_push:
        print(
            f"prepush-rebase-check: '{branch}' already has upstream '{upstream}' - pass (first-push rule only).",
            file=sys.stderr,
        )
        return 0

    if not origin_available:
        print(
            "prepush-rebase-check: no 'origin' remote available - skipping rebase check (pass).",
            file=sys.stderr,
        )
        return 0

    rc_f, out_f, err_f = _run_git("fetch", "origin")
    if rc_f != 0:
        print(
            f"prepush-rebase-check: 'git fetch origin' failed ({(err_f or out_f).strip() or 'no output'}); "
            "skipping rebase check (pass) - offline pushes must not be blocked.",
            file=sys.stderr,
        )
        return 0

    rc_a, _, _ = _run_git("merge-base", "--is-ancestor", "origin/main", "HEAD")
    if rc_a == 0:
        print(f"prepush-rebase-check: '{branch}' is based on latest origin/main - pass.", file=sys.stderr)
        return 0

    print(
        "prepush-rebase-check: FAILED - Branch is behind origin/main — rebase onto origin/main "
        "before the first push (git fetch origin main && git rebase origin/main).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
