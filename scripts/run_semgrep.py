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

import subprocess
import sys


def main() -> int:
    if sys.platform == "win32":
        print(
            "run_semgrep.py: semgrep-core cannot complete the incremental scan on Windows "
            "- skipping (semgrep is enforced on Linux CI and E2B sandboxes)",
            file=sys.stderr,
        )
        return 0

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
    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
