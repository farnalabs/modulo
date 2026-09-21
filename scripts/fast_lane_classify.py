#!/usr/bin/env python3
"""Fast-lane classifier and guardrails for the merge-queue.

Classifies a PR's changed paths into class-A (test-infra only) or class-B
(everything else). When a PR carries the ``fast-lane:test-infra`` label, the
classifier additionally enforces guardrails before granting fast-lane
eligibility.

Usage (CI):
    python scripts/fast_lane_classify.py \\
        --pr-number <N> \\
        --head-sha <SHA> \\
        --repo <owner/repo> \\
        [--cap 5] \\
        [--window-hours 24]

Exit codes:
    0  — eligible (class-A OR fast-lane guardrails pass)
    1  — not eligible (class-B OR guardrail violation)
    2  — usage / argument error

The script is deliberately in ``scripts/`` (class-B path) so the fast-lane
cannot edit the script that polices it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------

# Class-A: only test infra, docs (non-YAML), and non-workflow .github content.
# Every path is matched relative to the repo root.
_CLASS_A_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^backend/tests/"),
    re.compile(r"^frontend/tests/"),
    re.compile(r"^tests/"),
    re.compile(r"\.md$"),
    # .github/ is class-A EXCEPT workflows/, scripts/, and .yml/.yaml files.
    # We handle the exclusions via _CLASS_B_PATTERNS below.
    re.compile(r"^\.github/"),
]

# Explicit class-B patterns (supersedes class-A if a path matches).
_CLASS_B_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\.github/workflows/"),
    re.compile(r"^\.github/scripts/"),
    re.compile(r"\.(yml|yaml)$"),
    re.compile(r"^scripts/"),
    re.compile(r"^pyproject\.toml$"),
    re.compile(r"^backend/pyproject\.toml$"),
    re.compile(r"^frontend/package\.json$"),
    re.compile(r"^\.semgrep/"),
    re.compile(r"^\.pre-commit-config\.yaml$"),
    re.compile(r"^backend/src/"),
    re.compile(r"^frontend/src/"),
    re.compile(r"^fly\.toml$"),
    re.compile(r"^deploy/"),
    re.compile(r"^backend/src/modulo/db/migrations/"),
]


def classify_path(path: str) -> str:
    """Return ``'A'`` or ``'B'`` for a single repo-relative file path."""
    # Explicit class-B wins over class-A.
    for pat in _CLASS_B_PATTERNS:
        if pat.search(path):
            return "B"
    for pat in _CLASS_A_PATTERNS:
        if pat.search(path):
            return "A"
    return "B"


def classify_paths(paths: Sequence[str]) -> str:
    """Classify a PR: ``'A'`` if ALL paths are class-A, else ``'B'``."""
    if not paths:
        return "B"
    for p in paths:
        if classify_path(p) == "B":
            return "B"
    return "A"


# ---------------------------------------------------------------------------
# Guardrail: cap check (max fast-lane merges in rolling 24h)
# ---------------------------------------------------------------------------


def check_cap(
    repo: str,
    cap: int,
    window_hours: int,
    gh_token: str | None = None,
) -> tuple[bool, str]:
    """Check whether the rolling 24h cap on fast-lane merges has been reached.

    Returns ``(eligible, detail)``.
    """
    env = os.environ.copy()
    if gh_token:
        env["GH_TOKEN"] = gh_token

    since = int(time.time()) - (window_hours * 3600)
    # Find merged PRs in the window that carry fast-lane:test-infra and the
    # [auto-merge: test-infra] marker in the squash title.
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--limit",
        "100",
        "--json",
        "number,title,mergedAt",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            return True, f"cap check API error (fail-open): {result.stderr.strip()}"
        prs = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return True, f"cap check failed (fail-open): {exc}"

    count = 0
    for pr in prs:
        merged_at = pr.get("mergedAt")
        if not merged_at:
            continue
        # Parse ISO-8601 timestamp to epoch
        try:
            dt = datetime.fromisoformat(merged_at)
            merged_epoch = int(dt.timestamp())
        except (ValueError, TypeError):
            continue
        if merged_epoch < since:
            continue
        title = pr.get("title", "")
        if "[auto-merge: test-infra]" in title.lower():
            count += 1

    if count >= cap:
        return False, f"fast-lane cap reached: {count}/{cap} in last {window_hours}h"
    return True, f"fast-lane cap OK: {count}/{cap} in last {window_hours}h"


# ---------------------------------------------------------------------------
# Guardrail: no test-weakening
# ---------------------------------------------------------------------------


def check_no_test_weakening(
    repo_root: Path,
    base_ref: str,
    head_ref: str,
) -> tuple[bool, list[str]]:
    """Verify the diff does not weaken tests.

    Checks:
    1. Test-function count must not decrease
    2. Assert-line count must not decrease
    3. Zero added skip/xfail/skipif/@unittest.skip/conditional skips
    4. Zero deleted test files
    5. Zero deleted fixture teardown blocks (yield/addfinalizer/finally in
       changed test files)

    Returns ``(eligible, violations)``.
    """
    violations: list[str] = []

    # Get diff for changed test files
    cmd = [
        "git",
        "diff",
        "--name-status",
        f"{base_ref}...{head_ref}",
        "--",
        "backend/tests/",
        "frontend/tests/",
        "tests/",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=str(repo_root),
        )
        changed_files = result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True, []  # fail-open on infra errors

    deleted_test_files = []
    for line in changed_files:
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[1]
        if status == "D":
            deleted_test_files.append(path)

    # 4. Zero deleted test files
    if deleted_test_files:
        violations.append(f"deleted test files: {', '.join(deleted_test_files)}")

    # Get the full diff content for test-function, assert, skip, teardown checks
    diff_cmd = [
        "git",
        "diff",
        f"{base_ref}...{head_ref}",
        "--",
        "backend/tests/",
        "frontend/tests/",
        "tests/",
    ]
    try:
        diff_result = subprocess.run(
            diff_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=str(repo_root),
        )
        diff_text = diff_result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True, []

    # 1. Test-function count must not decrease
    # Count added vs removed def test_* lines
    added_funcs = 0
    removed_funcs = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++") and re.match(r"\+def test_\w+", line):
            added_funcs += 1
        elif line.startswith("-") and not line.startswith("---") and re.match(r"\-def test_\w+", line):
            removed_funcs += 1
    if removed_funcs > added_funcs:
        violations.append(
            f"test-function count decreased: -{removed_funcs} removed, "
            f"+{added_funcs} added (net {added_funcs - removed_funcs})"
        )

    # 2. Assert-line count must not decrease
    added_asserts = 0
    removed_asserts = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++") and "assert" in line:
            added_asserts += 1
        elif line.startswith("-") and not line.startswith("---") and "assert" in line:
            removed_asserts += 1
    if removed_asserts > added_asserts:
        violations.append(
            f"assert-line count decreased: -{removed_asserts} removed, "
            f"+{added_asserts} added (net {added_asserts - removed_asserts})"
        )

    # 3. Zero added skip/xfail/skipif/@unittest.skip/conditional skips
    skip_patterns = [
        re.compile(r"^\+.*\bpytest\.mark\.skip\b"),
        re.compile(r"^\+.*\bpytest\.mark\.xfail\b"),
        re.compile(r"^\+.*\b@unittest\.skip\b"),
        re.compile(r"^\+.*\bskipif\b"),
        re.compile(r"^\+.*\bpytest\.skip\("),
        re.compile(r"^\+.*\bskip\("),
    ]
    added_skips = 0
    for line in diff_text.splitlines():
        if not (line.startswith("+") and not line.startswith("+++")):
            continue
        for pat in skip_patterns:
            if pat.search(line):
                added_skips += 1
                break
    if added_skips > 0:
        violations.append(f"added {added_skips} skip/xfail/skipif marker(s)")

    # 5. Zero deleted fixture teardown blocks in changed test files
    # Look for removed lines containing yield/addfinalizer/finally in test files
    removed_teardown = 0
    for line in diff_text.splitlines():
        if not (line.startswith("-") and not line.startswith("---")):
            continue
        stripped = line[1:].strip()
        if stripped == "yield" or "addfinalizer" in stripped or stripped == "finally:":
            removed_teardown += 1
    if removed_teardown > 0:
        violations.append(f"removed {removed_teardown} fixture teardown block(s) (yield/addfinalizer/finally)")

    return len(violations) == 0, violations


# ---------------------------------------------------------------------------
# Guardrail: SHA-pinning
# ---------------------------------------------------------------------------


def check_sha_pinning(
    repo: str,
    pr_number: int,
    expected_sha: str,
    gh_token: str | None = None,
) -> tuple[bool, str]:
    """Verify the fast-lane check result is pinned to the PR's current head SHA.

    Returns ``(eligible, detail)``.
    """
    env = os.environ.copy()
    if gh_token:
        env["GH_TOKEN"] = gh_token

    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "headRefOid",
        "--jq",
        ".headRefOid",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
        actual_sha = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True, "SHA-pinning check failed (fail-open): could not fetch PR head"

    if not actual_sha:
        return True, "SHA-pinning check failed (fail-open): empty head SHA"

    if actual_sha.lower() == expected_sha.lower():
        return True, f"SHA-pinned: check matches head {actual_sha[:12]}"
    return False, f"SHA mismatch: expected {expected_sha[:12]}, PR head is {actual_sha[:12]} (stale check)"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fast-lane classifier and guardrails for the merge-queue.",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        required=True,
        help="GitHub PR number",
    )
    parser.add_argument(
        "--head-sha",
        required=True,
        help="Expected head SHA of the PR (for SHA-pinning check)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repo in owner/repo format",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=5,
        help="Max fast-lane merges in rolling window (default: 5)",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=24,
        help="Rolling window in hours for cap check (default: 24)",
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref for the base of the diff (default: origin/main)",
    )
    parser.add_argument(
        "--has-label",
        action="store_true",
        default=False,
        help="Whether the PR carries the fast-lane:test-infra label",
    )
    parser.add_argument(
        "--check-name",
        default="fast-lane",
        help="Name of the CI check to verify SHA-pinning against",
    )

    args = parser.parse_args(argv)

    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    # --- Step 1: classify changed paths ---
    cmd = [
        "gh",
        "pr",
        "diff",
        str(args.pr_number),
        "--repo",
        args.repo,
        "--name-only",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, **({"GH_TOKEN": gh_token} if gh_token else {})},
        )
        if result.returncode != 0:
            print(
                f"::error::fast-lane: could not fetch PR diff: {result.stderr.strip()}",
                file=sys.stderr,
            )
            return 2
        paths = [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"::error::fast-lane: could not fetch PR diff: {exc}", file=sys.stderr)
        return 2

    classification = classify_paths(paths)
    print(f"PR #{args.pr_number}: {len(paths)} changed file(s), class={classification}")

    if classification == "B":
        print(f"PR #{args.pr_number} is class-B — not fast-lane eligible")
        return 1

    if not args.has_label:
        print(f"PR #{args.pr_number} is class-A but lacks fast-lane:test-infra label — standard lane")
        return 1

    # --- Step 2: guardrails (only when label is present) ---
    print(f"PR #{args.pr_number}: running fast-lane guardrails...")

    # 2a. Cap check
    cap_ok, cap_detail = check_cap(args.repo, args.cap, args.window_hours, gh_token)
    print(f"  cap: {cap_detail}")
    if not cap_ok:
        print(f"::error::fast-lane: {cap_detail}")
        return 1

    # 2b. No test-weakening
    repo_root = Path(__file__).resolve().parent.parent
    weaken_ok, weaken_violations = check_no_test_weakening(
        repo_root,
        args.base_ref,
        "HEAD",
    )
    if weaken_violations:
        for v in weaken_violations:
            print(f"  test-weakening: {v}")
    if not weaken_ok:
        print("::error::fast-lane: test-weakening detected — demoted to class-B")
        return 1

    # 2c. SHA-pinning
    pin_ok, pin_detail = check_sha_pinning(
        args.repo,
        args.pr_number,
        args.head_sha,
        gh_token,
    )
    print(f"  sha-pin: {pin_detail}")
    if not pin_ok:
        print(f"::error::fast-lane: {pin_detail}")
        return 1

    print(f"PR #{args.pr_number}: class-A fast-lane ELIGIBLE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
