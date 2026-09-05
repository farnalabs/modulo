#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the incremental semgrep hook.

On Windows, semgrep-core cannot reliably scan the full `backend/src/`
directory with `--baseline-commit` (it hangs with "Failed to obtain target
files from semgrep-core"). Windows is a secondary local development platform;
semgrep is enforced on Linux in CI (ci.yml / deploy.yml) and in E2B sandbox
commits, so skipping the incremental pre-commit scan on Windows loses no
enforcement. On Linux/macOS this wrapper runs the exact command the hook used
before, unchanged.
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


def main() -> int:
    if sys.platform == "win32":
        print(
            "run_semgrep.py: semgrep-core cannot complete the incremental scan on Windows "
            "- skipping (semgrep is enforced on Linux CI and E2B sandboxes)",
            file=sys.stderr,
        )
        return 0

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
