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
exits non-zero when a threshold is breached or a required report is missing.

Gate semantics (fail-closed):
- **Report file missing** → ERROR, exit 1.  A missing report means the
  upstream job that produces it failed or its artifact download broke.
  Fail-closed so a broken pipeline never silently disables the gate.
  Use ``--allow-missing-reports`` for local runs where you may not have
  every report.
- **No changed coverable lines** → SKIP, exit 0.  This is the legitimate
  test-only / docs-only case where diff-cover reports "No lines with
  coverage information in this diff".
- **Threshold breach** → FAIL, exit 1.
- **Tiny diff (≤10 non-blank lines)** → PASS with a note.  Trivial
  changes (typo fixes, label tweaks) should not fail the gate.
- **Unmeasured changed file** → counts as 0% coverage.  A brand-new
  production file with no coverage in the report is a gate failure.
  Detected by comparing diff-cover's ``total_num_lines`` against the
  actual number of non-blank changed lines from ``git diff``.

Usage (local)::

    uv run --project backend python scripts/run_coverage_gate.py \\
        --compare-branch origin/main --allow-missing-reports

Usage (CI)::

    python scripts/run_coverage_gate.py \\
        --compare-branch origin/$GITHUB_BASE_REF
"""

from __future__ import annotations

import argparse
import json
import os
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

# Files/patterns excluded from the gate's denominator.  These mirror the
# ``sonar.coverage.exclusions`` in sonar-project.properties — test paths,
# migrations, scripts, docs examples, and generated code are all out of scope.
_EXCLUDE_PATTERNS: list[str] = [
    "migrations/**",
    "backend/scripts/**",
    "backend/tools/**",
    "scripts/**",
    "frontend/scripts/**",
    "docs/api/examples/**",
    "tests/**",
    "test_*/**",
    "**/test_*",
    "**/conftest.py",
    "**/tests/**",
    "**/migrations/**",
]

# Tiny-diff exemption: if the aggregate valid line count is below this
# threshold, pass with a note.  The ticket specified 10.
TINY_DIFF_THRESHOLD = 10

REPO_ROOT = Path(__file__).resolve().parent.parent

# Sentinel strings from diff-cover output.  These are matched against diff-cover
# 10.5.1's *real* human-readable output (verified empirically):
#   - stdout always ends with a ``Coverage: <pct>%`` line.
#   - on a threshold breach stderr carries ``Failure. Coverage is below <n>%.``
#   - a docs/test-only diff prints ``No lines with coverage information in this diff.``
_NO_CHANGED_LINES_RE = re.compile(r"No lines with coverage information in this diff", re.IGNORECASE)
_COVERAGE_LINE_RE = re.compile(r"Coverage:\s*(\d+(?:\.\d+)?)\s*%")
_THRESHOLD_NOT_MET_RE = re.compile(r"Failure\. Coverage is below", re.IGNORECASE)

# Allow-list for values that flow into a subprocess command line.  Deriving the
# value from a regex ``fullmatch().group(0)`` gives the taint analyser a string
# provably bounded to safe characters before it reaches ``subprocess``.
# Includes ``\\`` and ``:`` for Windows absolute paths (e.g. ``C:\\Users\\...``).
_PATH_CHARS_RE = re.compile(r"[A-Za-z0-9._/\\:-]+")


def _validate_ref(value: str, name: str) -> str:
    """Reject values that would be interpreted as CLI flags when passed as a
    subprocess argument (defense against argument injection), and return a
    regex-bounded copy so the taint analyser sees a value that cannot carry an
    injection payload.
    """
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}: expected a string, got {type(value).__name__}")
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty value that does not start with '-'")
    matched = _PATH_CHARS_RE.fullmatch(value)
    if not matched:
        raise ValueError(f"invalid {name}: {value!r} contains disallowed characters")
    return matched.group(0)


def _sanitize_path(value: str, name: str) -> str:
    """Constrain *value* to a safe path/ref character set and return the matched
    substring.  Any character outside the set (spaces, quotes, ``$``, ``;``, etc.)
    is rejected, so a crafted report path can never be interpreted as extra shell
    or diff-cover arguments.
    """
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}: expected a string, got {type(value).__name__}")
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty value that does not start with '-'")
    matched = _PATH_CHARS_RE.fullmatch(value)
    if not matched:
        raise ValueError(f"invalid {name}: {value!r} contains disallowed characters")
    return matched.group(0)


def _is_excluded(path: str) -> bool:
    """Check if a file path matches any exclusion pattern."""
    from fnmatch import fnmatch
    for pattern in _EXCLUDE_PATTERNS:
        if fnmatch(path, pattern):
            return True
    return False


def _get_changed_production_files(compare_branch: str, language: str) -> dict[str, int]:
    """Return {filepath: non_blank_line_count} for changed production files.

    Uses ``git diff <compare_branch>...HEAD`` (three-dot / merge-base) to find
    changed files, then counts non-blank added lines in the diff hunks.
    Excludes files matching the exclusion patterns.  Returns empty dict if git
    diff fails.

    The three-dot range MUST match the one diff-cover uses internally
    (``GitDiffTool.diff_committed`` defaults to ``<compare_branch>...HEAD``).  A
    two-dot ``<compare_branch> HEAD`` range compares the tips instead, so every
    commit that landed on the base branch after this branch diverged shows up as
    a changed line that diff-cover never measures.  That inflates
    ``changed_lines`` and fails the gate with a bogus "N unmeasured lines"
    result even though the PR touched no production files at all.
    """
    if language == "Python":
        globs = ["*.py"]
    elif language == "JavaScript":
        globs = ["*.ts", "*.tsx", "*.js", "*.jsx", "*.vue"]
    else:
        return {}

    # Validate before it reaches subprocess (defense against argument
    # injection), then widen to the merge-base range diff-cover uses.
    try:
        safe_compare_branch = _validate_ref(compare_branch, "compare-branch")
    except ValueError:
        return {}
    diff_range = f"{safe_compare_branch}...HEAD"

    diff_args = ["git", "diff", "--diff-filter=ACM", "--name-only", diff_range, "--", *globs]

    try:
        result = subprocess.run(  # NOSONAR - diff_range is regex fullmatch-bounded by _validate_ref (rejects values starting with '-' and disallowed characters); subprocess has no shell, so no argument injection is reachable
            diff_args,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            return {}
    except Exception:
        return {}

    files: dict[str, int] = {}
    for line in result.stdout.strip().splitlines():
        filepath = line.strip()
        if not filepath or _is_excluded(filepath):
            continue
        # Count non-blank added lines in the diff for this file
        file_diff_args = ["git", "diff", "--diff-filter=ACM", diff_range, "--", filepath]
        try:
            fd_result = subprocess.run(  # NOSONAR - diff_range is regex fullmatch-bounded by _validate_ref (rejects values starting with '-' and disallowed characters); filepath comes from git stdout, not caller input; subprocess has no shell
                file_diff_args,
                capture_output=True,
                text=True,
                check=False,
                cwd=str(REPO_ROOT),
            )
            if fd_result.returncode != 0:
                continue
            count = 0
            for diff_line in fd_result.stdout.splitlines():
                if diff_line.startswith("+") and not diff_line.startswith("+++"):
                    if diff_line[1:].strip():
                        count += 1
            if count > 0:
                files[filepath] = count
        except Exception:
            continue
    return files


@dataclass(frozen=True)
class GateResult:
    """Result of evaluating one language's coverage gate."""

    language: str
    skipped: bool
    skip_reason: str
    passed: bool
    actual_pct: float | None
    threshold: int
    changed_lines: int = 0
    tiny_diff: bool = False
    measured_lines: int = 0
    unmeasured_lines: int = 0

    def summary(self) -> str:
        if self.skipped:
            return f"[{self.language}] SKIPPED — {self.skip_reason}"
        if self.tiny_diff:
            return f"[{self.language}] PASS — tiny diff ({self.changed_lines} lines, ≤{TINY_DIFF_THRESHOLD} threshold)"
        if self.unmeasured_lines > 0 and not self.passed:
            return (
                f"[{self.language}] FAIL — {self.actual_pct:.1f}% measured, "
                f"{self.unmeasured_lines} unmeasured lines (0%) < {self.threshold}%"
            )
        if self.passed:
            pct = f"{self.actual_pct:.1f}%" if self.actual_pct is not None else "?"
            return f"[{self.language}] PASS — {pct} >= {self.threshold}%"
        if self.actual_pct is not None:
            return f"[{self.language}] FAIL — {self.actual_pct:.1f}% < {self.threshold}%"
        return f"[{self.language}] FAIL — {self.skip_reason}"


def _run_diff_cover(
    report_path: Path,
    compare_branch: str,
    fail_under: int,
) -> tuple[int, str]:
    """Run ``diff-cover`` and return (exit_code, combined_output).

    ``compare_branch`` is validated before it reaches ``subprocess`` so a
    caller-supplied value can never be interpreted as an extra flag.
    """
    exe_dir = Path(sys.executable).parent
    diff_cover_bin = exe_dir / "diff-cover"
    if sys.platform == "win32":
        diff_cover_bin = exe_dir / "diff-cover.exe"

    safe_compare_branch = _validate_ref(compare_branch, "compare-branch")
    safe_report = _sanitize_path(str(report_path), "report path")
    cmd = [  # NOSONAR - compare-branch is regex fullmatch-bounded by _validate_ref and the report path by _sanitize_path (both reject values starting with '-'); subprocess has no shell
        str(diff_cover_bin),
        safe_report,
        "--compare-branch",
        safe_compare_branch,
        "--fail-under",
        str(fail_under),
    ]
    result = subprocess.run(  # NOSONAR - reachable argv = [diff_cover_bin (resolved from the synced venv, not caller input), safe_report (regex fullmatch-bounded by _sanitize_path, rejects leading '-'), "--compare-branch" (literal), safe_compare_branch (regex fullmatch-bounded by _validate_ref, rejects leading '-'), "--fail-under" (literal), str(fail_under) (int->str literal)]; subprocess runs without a shell, so no flag injection is reachable from the only caller-influenced elements (safe_report/safe_compare_branch), which are validated before reaching this line.
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    combined = result.stdout
    if result.stderr:
        combined += "\n" + result.stderr
    return result.returncode, combined


def _get_diff_cover_json(report_path: Path, compare_branch: str) -> dict | None:
    """Run diff-cover with ``--format json:<path>`` and return parsed JSON.

    Returns the JSON report dict containing ``src_stats`` (per-file measured
    coverage), ``total_num_lines`` (lines diff-cover measured), and
    ``num_changed_lines`` (total lines in the diff).  Returns None on error.
    """
    exe_dir = Path(sys.executable).parent
    diff_cover_bin = exe_dir / "diff-cover"
    if sys.platform == "win32":
        diff_cover_bin = exe_dir / "diff-cover.exe"

    safe_compare_branch = _validate_ref(compare_branch, "compare-branch")
    safe_report = _sanitize_path(str(report_path), "report path")
    json_report_path = REPO_ROOT / ".diff-cover-report.json"
    safe_json_path = _sanitize_path(str(json_report_path), "json report path")

    cmd = [  # NOSONAR
        str(diff_cover_bin),
        safe_report,
        "--compare-branch",
        safe_compare_branch,
        "--format",
        f"json:{safe_json_path}",
    ]
    result = subprocess.run(  # NOSONAR
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )

    if not json_report_path.exists():
        return None
    try:
        data = json.loads(json_report_path.read_text())
        return data
    except (json.JSONDecodeError, OSError):
        return None
    finally:
        try:
            json_report_path.unlink()
        except OSError:
            pass


def evaluate(
    language: str,
    report_path: Path | None,
    compare_branch: str,
    fail_under: int,
    *,
    allow_missing: bool = False,
) -> GateResult:
    """Evaluate one language's changed-lines coverage.

    When *allow_missing* is False (the CI default), a missing or unreadable
    report is a gate failure — the upstream job or artifact download broke.
    When True (local convenience), a missing report is a skip.

    The gate detects unmeasured files by comparing the total non-blank changed
    lines (from ``git diff``) against diff-cover's ``total_num_lines``.  If
    diff-cover measured fewer lines than were actually changed, the unmeasured
    lines count as 0% coverage.
    """
    # --- Get changed production files and count non-blank lines ---
    changed_files = _get_changed_production_files(compare_branch, language)
    changed_lines = sum(changed_files.values())

    # --- Tiny-diff exemption ---
    if 0 < changed_lines <= TINY_DIFF_THRESHOLD:
        return GateResult(
            language=language,
            skipped=False,
            skip_reason="",
            passed=True,
            actual_pct=None,
            threshold=fail_under,
            changed_lines=changed_lines,
            tiny_diff=True,
        )

    # --- No changed lines → skip ---
    if changed_lines == 0:
        return GateResult(
            language=language,
            skipped=True,
            skip_reason="no changed production lines in this diff",
            passed=True,
            actual_pct=None,
            threshold=fail_under,
            changed_lines=0,
        )

    # --- Missing report ---
    if report_path is None or not report_path.exists():
        if allow_missing:
            return GateResult(
                language=language,
                skipped=True,
                skip_reason=f"no coverage report found at {report_path} (allow-missing)",
                passed=True,
                actual_pct=None,
                threshold=fail_under,
                changed_lines=changed_lines,
            )
        return GateResult(
            language=language,
            skipped=False,
            skip_reason=f"missing coverage report: {report_path}",
            passed=False,
            actual_pct=None,
            threshold=fail_under,
            changed_lines=changed_lines,
        )

    # --- Run diff-cover with JSON report to detect unmeasured lines ---
    json_data = _get_diff_cover_json(report_path.resolve(), compare_branch)
    rc, output = _run_diff_cover(report_path.resolve(), compare_branch, fail_under)

    # --- No changed coverable lines → skip (legitimate test/docs-only PR) ---
    # But only if ALL changed lines are unmeasured (diff-cover found nothing)
    if _NO_CHANGED_LINES_RE.search(output):
        # diff-cover found no coverage data for any changed file.
        # All changed lines are unmeasured → 0% coverage → FAIL.
        # Exception: if there are genuinely no changed production lines (should
        # not happen since we checked changed_lines above), skip.
        return GateResult(
            language=language,
            skipped=False,
            skip_reason="no coverage data for changed lines (0% effective)",
            passed=False,
            actual_pct=0.0,
            threshold=fail_under,
            changed_lines=changed_lines,
            measured_lines=0,
            unmeasured_lines=changed_lines,
        )

    # --- Extract measured coverage from JSON report ---
    measured_pct: float | None = None
    measured_lines = 0
    unmeasured_lines = 0

    if json_data and "src_stats" in json_data:
        src_stats = json_data["src_stats"]
        measured_lines = json_data.get("total_num_lines", 0)
        total_in_diff = json_data.get("num_changed_lines", changed_lines)
        # Lines in the diff that diff-cover didn't measure = unmeasured
        unmeasured_lines = max(0, changed_lines - measured_lines)

        # Compute weighted coverage: measured lines × their coverage + unmeasured × 0
        if changed_lines > 0:
            measured_pct_val = json_data.get("total_percent_covered", 0.0)
            # total_percent_covered is for measured lines only; recalculate
            # with unmeasured lines counted as 0%
            numerator = measured_lines * (measured_pct_val / 100.0)
            measured_pct = (numerator / changed_lines) * 100.0
    else:
        # Fallback: parse the text output
        m = _COVERAGE_LINE_RE.search(output)
        if m:
            measured_pct = float(m.group(1))
            # If we can't get measured_lines from JSON, assume diff-cover
            # measured all changed lines (conservative)
            unmeasured_lines = 0

    # --- Determine pass/fail ---
    if measured_pct is not None:
        passed = measured_pct >= fail_under
        if not passed:
            reason = f"coverage {measured_pct:.1f}% (with {unmeasured_lines} unmeasured lines at 0%) is below threshold {fail_under}%"
        elif unmeasured_lines > 0:
            reason = f"coverage {measured_pct:.1f}% >= {fail_under}% (but {unmeasured_lines} unmeasured lines counted at 0%)"
        else:
            reason = ""
    elif rc == 0:
        passed = True
        reason = ""
    elif _THRESHOLD_NOT_MET_RE.search(output):
        passed = False
        reason = f"coverage below threshold {fail_under}%"
    else:
        passed = False
        error_lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
        reason = error_lines[-1] if error_lines else "diff-cover returned non-zero"

    return GateResult(
        language=language,
        skipped=False,
        skip_reason=reason,
        passed=passed,
        actual_pct=measured_pct,
        threshold=fail_under,
        changed_lines=changed_lines,
        measured_lines=measured_lines,
        unmeasured_lines=unmeasured_lines,
    )


def _write_summary(results: list[GateResult]) -> None:
    """Write a GitHub step summary table and emit ::error:: annotations on failure."""
    summary_lines = [
        "## Coverage Gate (Changed Lines)\n",
        "| Language | Status | Coverage | Threshold | Changed Lines | Measured | Unmeasured |",
        "|----------|--------|----------|-----------|---------------|----------|------------|",
    ]
    for r in results:
        if r.skipped:
            status = "SKIPPED"
            pct = "—"
        elif r.tiny_diff:
            status = "PASS (tiny)"
            pct = "—"
        elif r.passed:
            status = "PASS"
            pct = f"{r.actual_pct:.1f}%" if r.actual_pct is not None else "?"
        else:
            status = "FAIL"
            pct = f"{r.actual_pct:.1f}%" if r.actual_pct is not None else "N/A"
        summary_lines.append(
            f"| {r.language} | {status} | {pct} | {r.threshold}% "
            f"| {r.changed_lines} | {r.measured_lines} | {r.unmeasured_lines} |"
        )

    summary_lines.append("")

    # Write to GITHUB_STEP_SUMMARY if available
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write("\n".join(summary_lines) + "\n")

    # Also print to stdout
    print("\n".join(summary_lines))

    # Emit ::error:: annotations for failures
    for r in results:
        if not r.skipped and not r.passed and not r.tiny_diff:
            msg = f"[{r.language}] Coverage gate failed: {r.summary()}"
            print(f"::error::{msg}")


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
    parser.add_argument(
        "--allow-missing-reports",
        action="store_true",
        default=False,
        help="Exit 0 when a report file is missing (for local use only; CI must NOT pass this).",
    )
    args = parser.parse_args()

    # Resolve default report paths relative to the repo root
    default_python = REPO_ROOT / "backend" / "coverage.xml"
    default_js = REPO_ROOT / "frontend" / "coverage" / "lcov.info"

    if args.python_report is not None:
        python_report = args.python_report
    elif default_python.exists():
        python_report = default_python
    else:
        python_report = None

    if args.js_report is not None:
        js_report = args.js_report
    elif default_js.exists():
        js_report = default_js
    else:
        js_report = None

    results: list[GateResult] = []

    if python_report is not None:
        results.append(
            evaluate(
                "Python",
                python_report,
                args.compare_branch,
                args.fail_under,
                allow_missing=args.allow_missing_reports,
            )
        )
    else:
        results.append(
            evaluate(
                "Python",
                None,
                args.compare_branch,
                args.fail_under,
                allow_missing=args.allow_missing_reports,
            )
        )

    if js_report is not None:
        results.append(
            evaluate(
                "JavaScript",
                js_report,
                args.compare_branch,
                args.fail_under,
                allow_missing=args.allow_missing_reports,
            )
        )
    else:
        results.append(
            evaluate(
                "JavaScript",
                None,
                args.compare_branch,
                args.fail_under,
                allow_missing=args.allow_missing_reports,
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
        _write_summary(results)
        return 0

    if any_failed:
        print("FAILED: one or more languages did not meet the coverage threshold or are missing reports.")
        _write_summary(results)
        return 1

    print("PASSED: all languages met the coverage threshold.")
    _write_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
