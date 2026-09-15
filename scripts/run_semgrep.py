#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the incremental semgrep hook.

On Linux/macOS this wrapper runs the exact command the hook used before,
unchanged: semgrep scans ``backend/src/`` with ``--baseline-commit=HEAD``.

On Windows, the full-directory baseline scan hangs (semgrep-core cannot
complete the diff for ~3400 files).  Instead, we detect the changed Python
files (staged for commit + unstaged working-tree changes) and scan ONLY
those files, with no ``--baseline-commit`` flag.  This catches any finding
in the files the developer is actually touching — the highest-value subset
— while remaining fast (~2 min vs 5+ min for the full scan, which times
out).  CI (Linux) still runs the full baseline scan on every push, so no
enforcement is lost.

The Windows path is fail-open on TOOL errors (timeout, crash, missing
semgrep): if semgrep cannot run, we warn loudly and exit 0 so the
developer is not blocked by a broken tool.  We are NEVER fail-open on
FINDINGS: if semgrep runs and finds violations, we exit non-zero.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Git-state env vars inherited from a running `git commit` (e.g. the relative
# `GIT_INDEX_FILE=.git/index`) break semgrep's `--baseline-commit` scan, which
# creates a temporary git worktree to diff against HEAD: git then resolves the
# inherited index path against the /tmp worktree and fails with "index file open
# failed: Not a directory". The baseline worktree must use the repo's own git
# context, so strip these before spawning semgrep. The hook still runs the full
# incremental scan and blocks on any new finding.
_GIT_STATE_ENV = {
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
}

_SEMGREP_TIMEOUT = 180  # seconds — scoped scan on a handful of files

_SEMGREP_WARN = """\
========================================================================
run_semgrep.py WARNING: semgrep did NOT complete on Windows.

  semgrep findings in your changed files were NOT checked locally.
  CI (Linux) will still catch them on push — do NOT claim lint-clean.

  Reason: {reason}
========================================================================"""


def _get_changed_py_files() -> list[str]:
    """Return relative paths of changed .py files under backend/src/.

    Checks both staged (index) and unstaged (working tree) changes so we
    catch everything the developer is touching, regardless of whether they
    ``git add``-ed first.
    """
    files: set[str] = set()
    for diff_flag in ("--cached", ""):
        cmd = [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
        ]
        if diff_flag:
            cmd.append(diff_flag)
        cmd.extend(["--", "backend/src/"])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if stripped.endswith(".py") and stripped.startswith("backend/src/"):
                files.add(stripped)
    return sorted(files)


def _run_windows() -> int:
    """Run semgrep on changed files only (scoped, no baseline)."""
    changed = _get_changed_py_files()
    if not changed:
        return 0

    env = {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV}
    cmd = [
        "uv",
        "run",
        "--project",
        "backend",
        "--no-sync",
        "semgrep",
        "scan",
        "--config=.semgrep/",
        "--error",
        *changed,
    ]

    try:
        result = subprocess.run(
            cmd,
            env=env,
            check=False,
            timeout=_SEMGREP_TIMEOUT,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print(
            _SEMGREP_WARN.format(reason=f"semgrep timed out after {_SEMGREP_TIMEOUT}s scanning {len(changed)} file(s)"),
            file=sys.stderr,
        )
        return 0
    except FileNotFoundError:
        print(
            _SEMGREP_WARN.format(reason="semgrep or uv not found on PATH"),
            file=sys.stderr,
        )
        return 0
    except Exception as exc:
        print(
            _SEMGREP_WARN.format(reason=f"unexpected error: {exc}"),
            file=sys.stderr,
        )
        return 0


def main() -> int:
    if sys.platform == "win32":
        return _run_windows()

    # pre-commit runs hooks from the repo root, so relative paths below resolve
    # correctly; the outer `uv run` in .pre-commit-config.yaml plus this inner
    # `uv run` is a redundant but harmless double hop that guarantees semgrep
    # executes with the locked backend environment.
    env = {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV}
    cmd = [
        "uv",
        "run",
        "--project",
        "backend",
        "--no-sync",
        "semgrep",
        "scan",
        "--config=.semgrep/",
        "--error",
        "--baseline-commit=HEAD",
        "backend/src/",
    ]
    result = subprocess.run(cmd, env=env, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
