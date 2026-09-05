#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the uv lockfile freshness check.

Replaces `powershell -NoProfile -File tools/check-uv-lock.ps1`. Runs
`uv lock --project backend --check` from the repo root. Exit 0 when the
lockfile is fresh, 1 when stale or with dependency conflicts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def main() -> int:
    print("Checking uv lockfile freshness...", file=sys.stderr)
    result = subprocess.run(
        ["uv", "lock", "--project", "backend", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        print("FAILED: uv lockfile is stale or has dependency conflicts", file=sys.stderr)
        return 1
    print("uv lockfile is fresh with no dependency conflicts", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
