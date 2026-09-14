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

Usage (local)::

    uv run --project backend python scripts/run_coverage_gate.py \\
        --compare-branch origin/main --allow-missing-reports

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

# Sentinel strings from diff-cover output.  These are matched against diff-cover
# 10.5.1's *real* human-readable output (verified empirically):
#   - stdout always ends with a ``Coverage: <pct>%`` line.
#   - on a threshold breach stderr carries ``Failure. Coverage is below <n>%.``
#   - a docs/test-only diff prints ``No lines with coverage information in this diff.``
# We parse this real text rather than inventing a format diff-cover never emits.
_NO_CHANGED_LINES_RE = re.compile(r"No lines with coverage information in this diff", re.IGNORECASE)
_COVERAGE_LINE_RE = re.compile(r"Coverage:\s*(\d+(?:\.\d+)?)\s*%")
_THRESHOLD_NOT_MET_RE = re.compile(r"Failure\. Coverage is below", re.IGNORECASE)


# Allow-list for values that flow into a subprocess command line.  Deriving the
# value from a regex ``fullmatch().group(0)`` gives the taint analyser a string
# provably bounded to safe characters before it reaches ``subprocess`` (the
# recognised remediation for argument-injection taint, pythonsecurity:S8705).
_PATH_CHARS_RE = re.compile(r"[A-Za-z0-9._/-]+")


def _validate_ref(value: str, name: str) -> str:
    """Reject values that would be interpreted as CLI flags when passed as a
    subprocess argument (defense against argument injection), and return a
    regex-bounded copy so the taint analyser sees a value that cannot carry an
    injection payload.

    Mirrors the established guard in ``scripts/backup.py`` / ``scripts/restore.py``.
    ``compare_branch`` originates from the ``--compare-branch`` CLI argument and is
    therefore attacker-influenced; a crafted value such as ``--extra-flag`` passed
    to ``diff-cover`` would be interpreted as an additional flag rather than a
    branch name.  Refusing anything that starts with ``-`` (and deriving the
    returned value from a strict allow-list match) closes that sink.
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
    or diff-cover arguments."""
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}: expected a string, got {type(value).__name__}")
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty value that does not start with '-'")
    matched = _PATH_CHARS_RE.fullmatch(value)
    if not matched:
        raise ValueError(f"invalid {name}: {value!r} contains disallowed characters")
    return matched.group(0)


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

    Uses the ``diff-cover`` CLI entry point from the same venv as the current
    Python (``sys.executable``'s sibling), not ``python -m diff_cover``
    (diff-cover is a console_scripts package without ``__main__``).
    """
    # diff-cover is installed as a console_scripts entry point.  Locate it
    # next to the current Python interpreter (inside the venv's Scripts/ or
    # bin/ directory).
    exe_dir = Path(sys.executable).parent
    diff_cover_bin = exe_dir / "diff-cover"
    if sys.platform == "win32":
        diff_cover_bin = exe_dir / "diff-cover.exe"

    # Pass the caller-supplied compare branch and threshold as discrete
    # argv elements rather than interpolating them into a single
    # ``--flag=value`` string.  ``compare_branch`` originates from the
    # ``--compare-branch`` CLI argument and is therefore attacker-influenced;
    # concatenating it into an argument would let a crafted value inject
    # additional ``diff-cover`` flags.  As separate elements the value can
    # never be interpreted as an extra argument (subprocess runs without a
    # shell), and ``_validate_ref`` rejects any value that could be read as a
    # flag (one starting with ``-``) before it gets here.
    safe_compare_branch = _validate_ref(compare_branch, "compare-branch")
    # The report path is also caller-supplied (or a default derived from argv);
    # sanitise it against a strict allow-list before it reaches subprocess.
    safe_report = _sanitize_path(str(report_path), "report path")
    # Every arg that reaches `cmd` is validated against a strict regex allow-list
    # in _validate_ref/_sanitize_path, which reject anything starting with '-' and
    # return a regex-bounded copy, so the argv-derived compare-branch and report
    # path can never reach a flag position; subprocess runs without a shell.
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
    *,
    allow_missing: bool = False,
) -> GateResult:
    """Evaluate one language's changed-lines coverage.

    When *allow_missing* is False (the CI default), a missing or unreadable
    report is a gate failure — the upstream job or artifact download broke.
    When True (local convenience), a missing report is a skip.
    """
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
            )
        return GateResult(
            language=language,
            skipped=False,
            skip_reason=f"missing coverage report: {report_path}",
            passed=False,
            actual_pct=None,
            threshold=fail_under,
        )

    rc, output = _run_diff_cover(report_path.resolve(), compare_branch, fail_under)

    # --- No changed coverable lines → skip (legitimate test/docs-only PR) ---
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
    # The regex only matches the real ``Coverage: <pct>%`` line and valid
    # numeric strings (\d+(?:\.\d+)?), so float() is safe here.
    actual_pct: float | None = None
    m = _COVERAGE_LINE_RE.search(output)
    if m:
        actual_pct = float(m.group(1))

    # --- Determine pass/fail ---
    # diff-cover exits 0 when coverage >= fail-under, 1 when below (or on a
    # genuine tool error).  We distinguish a threshold breach from a tool error
    # using the stderr sentinel ``Failure. Coverage is below <n>%.``
    if rc == 0:
        # Gate met.  diff-cover reports the percentage it compared, so a
        # missing value here means the output shape drifted — treat that as a
        # gate failure rather than silently passing.
        passed = actual_pct is not None
        reason = "" if passed else "diff-cover exited 0 but reported no coverage percentage"
    elif _THRESHOLD_NOT_MET_RE.search(output):
        passed = False
        pct = f"{actual_pct:.1f}" if actual_pct is not None else "?"
        reason = f"coverage {pct}% is below threshold {fail_under}%"
    else:
        # Non-zero for a reason other than threshold — tool error or
        # malformed report.  Capture the last non-empty line as the reason.
        passed = False
        error_lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
        reason = error_lines[-1] if error_lines else "diff-cover returned non-zero"

    return GateResult(
        language=language,
        skipped=False,
        skip_reason=reason,
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
        # No report path resolved at all (neither explicit nor default)
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
        return 0

    if any_failed:
        print("FAILED: one or more languages did not meet the coverage threshold or are missing reports.")
        return 1

    print("PASSED: all languages met the coverage threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
