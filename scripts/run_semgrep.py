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


def _get_changed_py_files() -> tuple[list[str], str | None]:
    """Return changed .py files under backend/src/ and any git failure reason.

    Checks both staged (index) and unstaged (working tree) changes so we
    catch everything the developer is touching, regardless of whether they
    ``git add``-ed first.

    Returns ``(files, git_error)``: ``git_error`` is ``None`` on success, or a
    non-empty reason string when a ``git diff`` invocation failed. A failed
    ``git diff`` (bare repo, permission error, corrupt index, or an initial
    commit where ``HEAD`` does not yet exist) yields no output; without the
    error surfaced, the hook would silently scan zero files and pass - a
    second silent fail-open path. The caller warns loudly on ``git_error``.
    """
    files: set[str] = set()
    git_error: str | None = None
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
        if result.returncode != 0:
            stderr = result.stderr.strip()
            git_error = stderr or f"{' '.join(cmd)} exited {result.returncode}"
            continue
        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if stripped.endswith(".py") and stripped.startswith("backend/src/"):
                files.add(stripped)
    return sorted(files), git_error


def _run_windows() -> int:
    """Run semgrep on changed files only (scoped, no baseline)."""
    changed, git_error = _get_changed_py_files()
    if git_error is not None:
        # Never silently skip: if we cannot determine the changed files, warn
        # loudly (still exit 0 - fail-open on TOOL, never on FINDINGS).
        print(
            _SEMGREP_WARN.format(reason=f"git change detection failed: {git_error}"),
            file=sys.stderr,
        )
    if not changed:
        return 0

    env = {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV}
    # On Windows, semgrep's pip package may not install a .exe wrapper, so
    # `uv run semgrep` fails with "Failed to spawn: program not found".
    # `python -m semgrep` is a dead stub (prints deprecation + exits 2).
    # CI (Linux) runs the full baseline scan on every push, so the local
    # Windows check is a convenience gate — fail open with a loud warning
    # rather than blocking the developer.
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

    def _is_spawn_failure(result: subprocess.CompletedProcess[str]) -> bool:
        """Return True when uv failed because the tool binary was not found."""
        return result.returncode != 0 and "Failed to spawn" in (result.stderr or "")

    try:
        result = subprocess.run(
            cmd,
            env=env,
            check=False,
            timeout=_SEMGREP_TIMEOUT,
            capture_output=True,
            text=True,
        )
        if _is_spawn_failure(result):
            print(
                _SEMGREP_WARN.format(
                    reason=(
                        "semgrep binary not found in venv (no .exe wrapper on "
                        "Windows). CI (Linux) still runs the full scan on push."
                    )
                ),
                file=sys.stderr,
            )
            return 0
        # Semgrep ran successfully — print its output and propagate its exit code.
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
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
    # Resource bounds prevent OOM on machines with limited free memory.
    # --jobs 1: single-threaded to cap RSS (semgrep-core spawns per-core workers
    #   that each load the full rule set; 8 workers x 845 files x 64 rules
    #   exhausts 16 GB on this machine).
    # --max-memory 2048: hard cap at 2 GB; semgrep exits gracefully instead of
    #   the OS OOM-killer taking the process (and potentially other agents).
    # --timeout 120: per-file timeout; prevents a single pathological file from
    #   blocking the whole hook indefinitely.
    # CI (Linux, Ubicloud 2-core) runs the full scan without these bounds and
    # has more headroom — the bounds here are a LOCAL-ONLY safety net.
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
        "--jobs",
        "1",
        "--max-memory",
        "2048",
        "--timeout",
        "120",
        "--baseline-commit=HEAD",
        "backend/src/",
    ]
    result = subprocess.run(cmd, env=env, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
