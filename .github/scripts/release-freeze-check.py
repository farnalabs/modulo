#!/usr/bin/env python3
"""
Release freeze check helper.

Reads the existence of the 'release-freeze' tag from the GitHub API.
Outputs a pipe-delimited verdict and message for the calling workflow.

Verdicts:
  frozen     - release-freeze tag exists (merge queue must pause)
  open       - no freeze tag (merge queue may proceed)
  fail-closed - persistent API error (must not be read as "open")

Usage:
  python .github/scripts/release-freeze-check.py
"""

import os
import subprocess
import sys
import time

# Retry policy: 3 attempts, exponential backoff (2s, 4s, 8s) = max ~14s
MAX_ATTEMPTS = 3
BASE_BACKOFF = 2


def _run_gh_api() -> tuple[int, str, str]:
    """Run gh api to check for release-freeze tag."""
    cmd = [
        "gh",
        "api",
        f"repos/{os.environ.get('GITHUB_REPOSITORY', 'farnalabs/modulo')}/git/ref/tags/release-freeze",
        "--jq",
        ".ref",
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "gh api timed out"
    except OSError as exc:
        return -1, "", f"gh not available: {exc}"


def check_freeze() -> tuple[str, str]:
    """
    Check if release-freeze tag exists.

    Returns:
        (verdict, message) where verdict is one of:
        - "frozen"
        - "open"
        - "fail-closed"
    """
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rc, stdout, stderr = _run_gh_api()
        if rc == 0:
            # Tag exists -> frozen
            if "release-freeze" in stdout:
                return "frozen", "release-freeze tag present"
            # Unexpected success without tag
            return "open", "no release-freeze tag"
        if rc == 404:
            # 404 = tag not found -> open
            return "open", "no release-freeze tag"
        # Other error (rate limit, network, auth) -> retry
        last_err = f"gh api exit {rc}: {stderr or stdout}"
        if attempt < MAX_ATTEMPTS:
            time.sleep(BASE_BACKOFF * (2 ** (attempt - 1)))
    # All attempts failed -> fail-closed
    return "fail-closed", f"persistent API error after {MAX_ATTEMPTS} attempts: {last_err}"


def main() -> int:
    verdict, message = check_freeze()
    print(f"{verdict}|{message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
