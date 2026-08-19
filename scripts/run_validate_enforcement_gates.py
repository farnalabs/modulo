#!/usr/bin/env python3
"""Cross-platform pre-commit wrapper that validates the enforcement gates.

Replaces `powershell -NoProfile -File tools/validate-enforcement-gates.ps1`.
Checks that the CI and delivery enforcement gates are structurally intact:

1. No ``continue-on-error: true`` on CI validation jobs in .github/workflows/ci.yml
2. verify-main.ps1 (devtools sibling) uses Fail (not Warn) for vue-tsc / npm audit / pip-audit
3. smoke-test.ps1 (devtools sibling) has a Playwright @smoke step
4. AGENTS.md has a "Non-Negotiable Enforcement Gates" section

The devtools sibling path is resolved the same way the .ps1 does: via
``git rev-parse --git-common-dir`` (which, for a worktree, points at the main
checkout's .git) so the sibling `devtools` next to the primary modulo checkout
is located even when running from a worktree branch.

Exit 0 = all gates intact. Exit 1 = a violation found.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _resolve_devtools_root() -> str:
    # Resolve the caller's checkout so proposed worktree changes are validated.
    git_root = _git("rev-parse", "--show-toplevel")
    modulo_root = git_root or os.path.dirname(REPO_ROOT)

    # Devtools is a sibling of the primary checkout, while worktrees are nested
    # beneath that checkout. Resolve the common Git directory for that sibling.
    common_dir = _git("rev-parse", "--git-common-dir")
    if common_dir:
        common_dir = os.path.normpath(common_dir)
        # For a worktree, --git-common-dir is a relative path like
        # "<main>/../.git"; resolve it against the current working directory.
        if not os.path.isabs(common_dir):
            common_dir = os.path.abspath(common_dir)
        primary_modulo_root = os.path.dirname(common_dir)
        devtools_root = os.path.join(os.path.dirname(primary_modulo_root), "devtools")
    else:
        devtools_root = os.path.join(os.path.dirname(modulo_root), "devtools")
    return os.path.normpath(devtools_root)


def _read(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        # The harness .ps1 files (gate.ps1, verify-main.ps1) may carry
        # Windows-1252 bytes; decode leniently like PowerShell's Get-Content.
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def main() -> int:
    exit_code = 0
    devtools_root = _resolve_devtools_root()

    def check(label: str, ok: bool, fix_hint: str) -> None:
        nonlocal exit_code
        if not ok:
            print(f"  [FAIL] {label}", file=sys.stderr)
            print(f"         Fix: {fix_hint}", file=sys.stderr)
            exit_code = 1
        else:
            print(f"  [PASS] {label}", file=sys.stderr)

    # Check 1: No continue-on-error: true on CI validation jobs.
    ci_yml = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
    ci_content = _read(ci_yml)
    check(
        "No continue-on-error on CI validation jobs",
        ci_content is not None and not re.search(r"(?m)^\s*continue-on-error:\s*true\s*$", ci_content),
        "Remove continue-on-error: true from validation steps in .github/workflows/ci.yml",
    )

    # Check 2: verify-main.ps1 must use Fail (not Warn) for vue-tsc / npm audit / pip-audit.
    verify_main = os.path.join(devtools_root, "harness", "tools", "verify-main.ps1")
    verify_content = _read(verify_main)

    check(
        "verify-main.ps1 uses Fail for vue-tsc",
        verify_content is not None and not re.search(r"Warn.*vue-tsc", verify_content),
        f"Change Warn to Fail for vue-tsc check in {verify_main}",
    )
    check(
        "verify-main.ps1 uses Fail for npm audit",
        verify_content is not None and not re.search(r"Warn.*npm audit", verify_content),
        f"Change Warn to Fail for npm audit check in {verify_main}",
    )
    check(
        "verify-main.ps1 uses Fail for pip-audit",
        verify_content is not None and not re.search(r"Warn.*pip-audit", verify_content),
        f"Change Warn to Fail for pip-audit check in {verify_main}",
    )

    # Check 3: smoke-test.ps1 has a Playwright @smoke step. The local
    # gate.ps1 merge path was retired (2026-08-18); the Playwright @smoke E2E
    # gate now lives in devtools' smoke-test.ps1.
    smoke_test = os.path.join(devtools_root, "harness", "tools", "smoke-test.ps1")
    smoke_content = _read(smoke_test)
    check(
        "smoke-test.ps1 has Playwright @smoke step",
        smoke_content is not None and ("playwright" in smoke_content or "@smoke" in smoke_content),
        f"Add Playwright @smoke step to {smoke_test}",
    )

    # Check 4: AGENTS.md has the enforcement gates section.
    agents_md = os.path.join(REPO_ROOT, "AGENTS.md")
    agents_content = _read(agents_md)
    check(
        "AGENTS.md has Non-Negotiable Enforcement Gates section",
        agents_content is not None and "Non-Negotiable Enforcement Gates" in agents_content,
        "Add ## Non-Negotiable Enforcement Gates section to AGENTS.md",
    )

    if exit_code == 0:
        print("\nAll enforcement gates intact.", file=sys.stderr)
    else:
        print("\nENFORCEMENT VIOLATION FOUND - see above.", file=sys.stderr)
        print("These gates are STRUCTURALLY PROTECTED and must not be weakened.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
