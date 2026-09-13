#!/usr/bin/env python3
"""Fast wrapper around the test-style scanner suite.

Runs only the lenses against test files that changed versus a given ref,
using MODULO_TEST_STYLE_SCOPE + pytest-xdist for speed.  When no test
files changed, exits immediately with 0.

Usage:
    python scripts/run_test_suite_quality.py [--changed-files [REF]]
                                             [--lenses EXPR]
                                             [--jobs N]
                                             [--full]
                                             [--ref REF]
                                             [--timeout SECS]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
TEST_PATTERN = "backend/tests/**/*.py"


def _git(*args: str) -> str | None:
    """Run a git command and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(REPO_ROOT),
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _collect_changed_test_files(ref: str) -> list[str]:
    """Return repo-relative paths of test files changed versus *ref*.

    Combines two diffs:
    1. ref...HEAD  (committed changes on this branch)
    2. HEAD        (uncommitted / working-tree changes)
    Both are restricted to backend/tests/**/*.py with --diff-filter=ACMR.
    """
    files: set[str] = set()

    # Committed changes on this branch vs the comparison ref
    raw = _git("diff", "--name-only", "--diff-filter=ACMR", f"{ref}...HEAD")
    if raw:
        for line in raw.splitlines():
            if line.startswith("backend/tests/") and line.endswith(".py"):
                files.add(line)

    # Uncommitted / working-tree changes
    raw = _git("diff", "--name-only", "--diff-filter=ACMR", "HEAD")
    if raw:
        for line in raw.splitlines():
            if line.startswith("backend/tests/") and line.endswith(".py"):
                files.add(line)

    return sorted(files)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the test-style scanner fast (scoped + parallel).",
    )
    parser.add_argument(
        "--changed-files",
        nargs="?",
        const="origin/main",
        default=None,
        metavar="REF",
        help="Scan only test files changed vs REF (default: origin/main). Flag without value uses origin/main.",
    )
    parser.add_argument(
        "--lenses",
        default=None,
        metavar="EXPR",
        help='Forward as pytest -k "EXPR" to run only matching lens tests.',
    )
    parser.add_argument(
        "--jobs",
        default="auto",
        metavar="N",
        help="Parallelism for pytest-xdist (default: auto, capped at 8).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore scope; run the whole suite (serial or via --jobs).",
    )
    parser.add_argument(
        "--ref",
        default=None,
        metavar="REF",
        help="Override the comparison ref (only used with --changed-files).",
    )
    parser.add_argument(
        "--timeout",
        default=120,
        type=int,
        metavar="SECS",
        help="Per-test timeout in seconds (default: 120).",
    )

    args = parser.parse_args()

    # Determine scope
    scope_files: list[str] | None = None
    if not args.full:
        ref = args.ref or args.changed_files or "origin/main"
        scope_files = _collect_changed_test_files(ref)

    # Determine jobs
    jobs = args.jobs
    if jobs == "auto":
        cpu = os.cpu_count() or 4
        jobs = str(min(8, cpu))

    # Build pytest command
    cmd: list[str] = [
        sys.executable,
        "-m",
        "pytest",
        "tests/architecture/test_test_suite_quality.py",
        "-q",
        "--tb=short",
        f"--timeout={args.timeout}",
        "-p",
        "no:warnings",
    ]

    if scope_files:
        # Set the scope env var for the child process
        env_scope = os.pathsep.join(str(BACKEND / f) for f in scope_files)
        print(f"Scoped to {len(scope_files)} changed test file(s):")
        for f in scope_files:
            print(f"  {f}")
    else:
        env_scope = ""
        if not args.full:
            print("No scoped test files changed — nothing to scan.")
            return 0

    if args.lenses:
        cmd.extend(["-k", args.lenses])

    # Add xdist parallelism unless running a single lens expression
    if not args.lenses or " and " not in (args.lenses or ""):
        cmd.extend(["-n", jobs])

    print(f"Jobs: {jobs}")
    print(f"Command: {' '.join(cmd)}")

    env = os.environ.copy()
    if env_scope:
        env["MODULO_TEST_STYLE_SCOPE"] = env_scope
    elif not args.full:
        # Empty scope + not full = nothing to scan (already returned above)
        pass

    t0 = time.monotonic()
    result = subprocess.run(cmd, cwd=str(BACKEND), env=env, check=False)
    elapsed = time.monotonic() - t0

    print(f"\nElapsed: {elapsed:.1f}s")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
