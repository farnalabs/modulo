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
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")


def _changed_unit_tests() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--cached", "--diff-filter=ACMR"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    paths = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("backend/tests/unit/") and line.endswith(".py"):
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

    cmd = ["uv", "run", "--no-sync", "pytest", "--tb=short", "-q", "--timeout=120", *test_paths]
    result = subprocess.run(cmd, cwd=BACKEND_DIR)
    if result.returncode != 0:
        print("FAILED: Changed tests did not pass", file=sys.stderr)
        return 1
    print("All changed tests pass", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
