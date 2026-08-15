#!/usr/bin/env python3
"""Cross-platform pre-commit + CI wrapper for the vulture dead-code gate.

Vulture is a pure-Python static analyser (no native core), so unlike
run_semgrep.py this wrapper runs identically on Windows, Linux and macOS and
is BLOCKING everywhere. It exists to give both invocation contexts — the
pre-commit hook (which runs from the repo root) and the CI backend-lint job
(which uses `working-directory: backend`) — a single, path-independent entry
point. The wrapper locates the repo root from its own location and passes
absolute paths to vulture, so the `.vulture_whitelist.py` at the repo root
resolves identically in both.

The whitelist is vulture's documented mechanism: a Python file passed as an
additional PATH whose `__all__ = [...]` list feeds vulture's used_names set.
There is no `--whitelist` CLI flag in vulture 2.16.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Git-state env vars inherited from a running `git commit` (e.g. the relative
# `GIT_INDEX_FILE=.git/index`) are harmless to vulture (unlike semgrep's
# baseline worktree scan), but strip them anyway for parity with run_semgrep.py
# so both wrappers present a clean environment.
_GIT_STATE_ENV = {
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
}


def _repo_root() -> str:
    """Return the repository root (the parent of this script's directory)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    root = _repo_root()
    src = os.path.join(root, "backend", "src", "modulo")
    whitelist = os.path.join(root, ".vulture_whitelist.py")
    if not os.path.isdir(src) or not os.path.isfile(whitelist):
        print(
            f"run_vulture.py: could not resolve repo layout (src={src!r}, whitelist={whitelist!r})",
            file=sys.stderr,
        )
        return 2

    env = {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV}
    cmd = [
        "uv",
        "run",
        "--project",
        "backend",
        "--no-sync",
        "vulture",
        src,
        whitelist,
        "--min-confidence",
        "80",
    ]
    # Pin the uv subprocess cwd to the repo root so `--project backend`
    # resolves identically whether this wrapper was invoked from the repo root
    # (pre-commit) or from backend/ (CI's working-directory).
    result = subprocess.run(cmd, env=env, cwd=root)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
