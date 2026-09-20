#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper that runs pytest on changed unit-test files.

Replaces `powershell -NoProfile -File tools/run-changed-tests.ps1`. Finds
staged unit-test files under backend/tests/unit/, runs pytest on them from
backend/ (so backend/.env resolves for Settings()), and fails if any fail.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
BACKEND_DIR = str(Path(REPO_ROOT) / "backend")

# The shared hook-context helper lives in ``scripts/``; ensure the repo root is
# importable when this file is executed directly as a script.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from scripts.git_hook_env import strip_git_hook_context_vars


def _changed_unit_tests() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--cached", "--diff-filter=ACMR"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    paths = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not (line.startswith("backend/tests/unit/") and line.endswith(".py")):
            continue
        # pytest support files (conftest.py, __init__.py) collect zero tests
        # when passed explicitly (exit 5) and are auto-loaded whenever sibling
        # test files run, so they never belong in the explicit path list.
        if Path(line).name in ("conftest.py", "__init__.py"):
            continue
        paths.append(line)
    return paths


def main() -> int:
    changed = _changed_unit_tests()
    if not changed:
        print(
            "No unit test files changed - skipping changed-test run (integration tests are covered by CI with Docker)",
            file=sys.stderr,
        )
        return 0

    # Strip the repo-root 'backend/' prefix so the paths resolve from backend/.
    test_paths = [p[len("backend/") :] for p in changed]
    print(f"Running tests for changed files: {' '.join(test_paths)}", file=sys.stderr)

    cmd = ["uv", "run", "--no-sync", "python", "-m", "pytest", "--tb=short", "-q", "--timeout=120", *test_paths]
    env = strip_git_hook_context_vars(os.environ)
    # Only fall back to USERPROFILE when HOME is genuinely absent (Windows);
    # an empty HOME breaks uv/git resolution in the subprocess.
    if "HOME" not in env and "USERPROFILE" in env:
        env["HOME"] = env["USERPROFILE"]
    result = subprocess.run(cmd, cwd=BACKEND_DIR, check=False, env=env)
    if result.returncode != 0:
        print("FAILED: Changed tests did not pass", file=sys.stderr)
        return 1
    print("All changed tests pass", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
