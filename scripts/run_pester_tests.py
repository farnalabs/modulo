#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper for the Pester PowerShell test suite.

Pester is inherently PowerShell-only — there is no Linux/macOS equivalent.
This wrapper therefore:
  - On non-Windows (sys.platform != "win32"): prints a skip notice and exits 0
    (mirroring how run_semgrep.py skips on Windows, since Pester cannot run
    outside PowerShell). The Windows tool suite still runs in CI/Windows dev.
  - On Windows: shells out to the existing pinned-Pester runner
    ``tools/run-pester-tests.ps1``, which downloads and pins Pester 5.7.1 from
    the PowerShell Gallery (hash-verified) and runs the tools/tests suite.

``tools/run-pester-tests.ps1`` is deliberately KEPT (not deleted) because it
encapsulates the pinned Pester bootstrap logic that has no Python equivalent.
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PESTER_SCRIPT = os.path.join(REPO_ROOT, "tools", "run-pester-tests.ps1")


def main() -> int:
    if sys.platform != "win32":
        print(
            "run_pester_tests.py: Pester is PowerShell-only and has no Linux/macOS "
            "equivalent - skipping (the Pester tool suite runs on Windows)",
            file=sys.stderr,
        )
        return 0

    if not os.path.isfile(PESTER_SCRIPT):
        print(
            f"run_pester_tests.py: {PESTER_SCRIPT} not found - skipping",
            file=sys.stderr,
        )
        return 0

    cmd = ["powershell", "-NoProfile", "-File", PESTER_SCRIPT]
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
