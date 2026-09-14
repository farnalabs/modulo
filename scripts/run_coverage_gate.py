#!/usr/bin/env python3
"""Changed-lines coverage gate — enforces a per-PR coverage threshold on the
lines each PR actually changes (the analogue of SonarCloud's ``new_coverage``).

Why this exists: farnalabs/modulo is on free SonarCloud, which does NOT allow
customising or assigning quality gates.  SonarCloud still computes and
displays coverage, but we cannot make it block on a threshold.  We enforce it
ourselves in CI via this script.

The script runs ``diff-cover`` against the backend Cobertura XML report and
the frontend LCOV report *separately*, using ``--compare-branch`` to diff
against the PR's base branch.  It prints a clear summary per language and
exits non-zero only when a real threshold breach occurs.

Skips gracefully (exit 0) when:
- A report file is absent (e.g. backend-only PR has no LCOV).
- There are no changed coverable lines (e.g. docs-only or test-only PRs
  where diff-cover reports "No lines with coverage information in this diff").

Usage (local)::

    uv run --project backend python scripts/run_coverage_gate.py \\
        --compare-branch origin/main

Usage (CI)::

    python scripts/run_coverage_gate.py \\
        --compare-branch origin/$GITHUB_BASE_REF
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Threshold constant — the single source of truth for the coverage floor.
# Change this value to adjust the gate across all PRs.
# ---------------------------------------------------------------------------
COVERAGE_THRESHOLD = 90

REPO_ROOT = Path(__file__).resolve().parent.parent

# Sentinel strings from diff-cover output
_NO_CHANGED_LINES_RE = re.compile(r"No lines with coverage information in this diff", re.IGNORECASE)
_COVERAGE_LINE_RE = re.compile(r"Coverage on lines differing from.*?:\s*(\d+(?:\.\d+)?)\s*%")
_THRESHOLD_NOT_MET_RE = re.compile(r"Coverage threshold not met", re.IGNORECASE)


@dataclass(frozen=True)
class GateResult:
    """Result of evaluating one language's coverage gate."""

    language: str
    skipped: bool
    skip_reason: str
    passed: bool
    actual_pct: float | None
    threshold: int

    def summary(self) -> str:
        if self.skipped:
            return f"[{self.language}] SKIPPED — {self.skip_reason}"
        if self.passed:
            return f"[{self.language}] PASS — {self.actual_pct:.1f}% >= {self.threshold}%"
        return f"[{self.language}] FAIL — {self.actual_pct:.1f}% < {self.threshold}%"


def _run_diff_cover(
    report_path: Path,
    compare_branch: str,
    fail_under: int,
) -> tuple[int, str]:
    """Run ``diff-cover`` and return (exit_code, combined_output)."""
    cmd = [
        sys.executable,
        "-m",
        "diff_cover",
        str(report_path),
        f"--compare-branch={compare_branch}",
        f"--fail-under={fail_under}",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    # diff-cover writes progress/status to stdout, errors to stderr.
    combined = result.stdout
    if result.stderr:
        combined += "\n" + result.stderr
    return result.returncode, combined


def evaluate(
    language: str,
    report_path: Path | None,
    compare_branch: str,
    fail_under: int,
) -> GateResult:
    """Evaluate one language's changed-lines coverage."""
    # --- Missing report → skip ---
    if report_path is None or not report_path.exists():
        return GateResult(
            language=language,
            skipped=True,
            skip_reason=f"no coverage report found at {report_path}",
            passed=True,
            actual_pct=None,
            threshold=fail_under,
        )

    rc, output = _run_diff_cover(report_path, compare_branch, fail_under)

    # --- No changed coverable lines → skip ---
    if _NO_CHANGED_LINES_RE.search(output):
        return GateResult(
            language=language,
            skipped=True,
            skip_reason="no changed coverable lines in this diff",
            passed=True,
            actual_pct=None,
            threshold=fail_under,
        )

    # --- Extract actual coverage percentage ---
    # The regex only matches valid numeric strings (\d+(?:\.\d+)?), so
    # float() is safe here — no ValueError guard needed.
    actual_pct: float | None = None
    m = _COVERAGE_LINE_RE.search(output)
    if m:
        actual_pct = float(m.group(1))

    # --- Determine pass/fail ---
    # diff-cover exits 0 when coverage >= fail-under, 1 when below.
    # We also check the output for extra robustness.
    if rc == 0 and actual_pct is not None:
        passed = True
    elif rc != 0 and _THRESHOLD_NOT_MET_RE.search(output):
        passed = False
    elif rc != 0:
        # Non-zero for a reason other than threshold — treat as failure
        passed = False
    else:
        passed = actual_pct is not None and actual_pct >= fail_under

    return GateResult(
        language=language,
        skipped=False,
        skip_reason="",
        passed=passed,
        actual_pct=actual_pct,
        threshold=fail_under,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Changed-lines coverage gate using diff-cover.",
    )
    parser.add_argument(
        "--compare-branch",
        default="origin/main",
        help="Branch to diff against (default: origin/main).",
    )
    parser.add_argument(
        "--fail-under",
        type=int,
        default=COVERAGE_THRESHOLD,
        help=f"Minimum coverage percentage for changed lines (default: {COVERAGE_THRESHOLD}).",
    )
    parser.add_argument(
        "--python-report",
        type=Path,
        default=None,
        help="Path to the backend Cobertura XML coverage report.",
    )
    parser.add_argument(
        "--js-report",
        type=Path,
        default=None,
        help="Path to the frontend LCOV coverage report.",
    )
    args = parser.parse_args()

    # Resolve default report paths relative to the repo root
    default_python = REPO_ROOT / "backend" / "coverage.xml"
    default_js = REPO_ROOT / "frontend" / "coverage" / "lcov.info"

    python_report = (
        args.python_report if args.python_report is not None else (default_python if default_python.exists() else None)
    )
    js_report = args.js_report if args.js_report is not None else (default_js if default_js.exists() else None)

    results: list[GateResult] = []

    if python_report is not None:
        results.append(evaluate("Python", python_report, args.compare_branch, args.fail_under))
    else:
        results.append(
            GateResult(
                language="Python",
                skipped=True,
                skip_reason="no backend coverage report (coverage.xml) found",
                passed=True,
                actual_pct=None,
                threshold=args.fail_under,
            )
        )

    if js_report is not None:
        results.append(evaluate("JavaScript", js_report, args.compare_branch, args.fail_under))
    else:
        results.append(
            GateResult(
                language="JavaScript",
                skipped=True,
                skip_reason="no frontend coverage report (lcov.info) found",
                passed=True,
                actual_pct=None,
                threshold=args.fail_under,
            )
        )

    # --- Summary ---
    print("\n=== Coverage Gate Summary ===")
    for r in results:
        print(f"  {r.summary()}")
    print()

    all_skipped = all(r.skipped for r in results)
    any_failed = any(not r.skipped and not r.passed for r in results)

    if all_skipped:
        print("No coverage data to check — gate passed (all languages skipped).")
        return 0

    if any_failed:
        print("FAILED: one or more languages did not meet the coverage threshold.")
        return 1

    print("PASSED: all languages met the coverage threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
