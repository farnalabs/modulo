#!/usr/bin/env python3
"""Changed-lines coverage gate — enforces a per-PR coverage threshold on the
lines each PR actually changes (the analogue of SonarCloud's ``new_coverage``).

Why this exists: farnalabs/modulo is on free SonarCloud, which does NOT allow
customising or assigning quality gates.  SonarCloud still computes and
displays coverage, but we cannot make it block on a threshold.  We enforce it
ourselves in CI via this script.

The script runs ``diff-cover`` against the backend Cobertura XML report and
the frontend LCOV report *separately*, using ``--compare-branch`` to diff
against the PR's base branch.  The comparison uses diff-cover's merge-base
semantics (the branch's actual changes, not main's newer content), and the
frontend LCOV paths are normalised before the run because vitest emits them
relative to ``frontend/`` rather than the repo root.  It prints a clear
summary per language and exits non-zero when a threshold is breached or a
required report is missing.

Gate semantics (fail-closed):
- **Report file missing** → ERROR, exit 1.  A missing report means the
  upstream job that produces it failed or its artifact download broke.
  Fail-closed so a broken pipeline never silently disables the gate.
  Use ``--allow-missing-reports`` for local runs where you may not have
  every report.
- **No changed production lines** → SKIP, exit 0.  A test-only or docs-only
  diff has no production files once the exclusions are applied.
- **Changed production lines, but no coverage data for them** → FAIL,
  exit 1.  If production lines changed but diff-cover cannot match any of
  them to the coverage report (the report does not contain those files),
  every changed line counts as unmeasured coverage, i.e. 0%.
- **Threshold breach** → FAIL, exit 1.
- **Tiny diff (≤10 non-blank lines)** → PASS with a note.  Trivial
  changes (typo fixes, label tweaks) should not fail the gate.
- **Unmeasured changed file** → counts as 0% coverage.  A brand-new
  production file with no coverage in the report is a gate failure.
  Detected by comparing the production-only measured lines reported by
  diff-cover against the non-blank changed production lines from
  ``git diff`` (both scoped to the same file set).

Usage (local)::

    uv run --project backend python scripts/run_coverage_gate.py \\
        --compare-branch origin/main --allow-missing-reports

Usage (CI)::

    python scripts/run_coverage_gate.py \\
        --compare-branch origin/$GITHUB_BASE_REF
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
_DIFF_COVER_TOTAL_RE = re.compile(r"Total:\s*(\d+)\s+line", re.IGNORECASE)

# ``git diff`` filter and language pathspecs for changed-file discovery.
_DIFF_FILTER = "--diff-filter=ACM"
_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "Python": ("*.py",),
    "JavaScript": ("*.ts", "*.tsx", "*.js", "*.jsx", "*.vue"),
}

# Directory (relative to the repo root) that the frontend LCOV report's
# relative ``SF:`` paths are resolved against.
DEFAULT_JS_SRC_ROOT = "frontend"

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

    return any(fnmatch(path, pattern) for pattern in _EXCLUDE_PATTERNS)


def _safe_repo_path(value: str) -> str | None:
    """Constrain a repo-relative path to a safe character set.

    Returns the matched value (so the taint analyser sees a string bounded by
    the regex) or ``None`` when the path is empty or contains disallowed
    characters.  Values that start with ``-`` are rejected so a path can never
    be mistaken for a CLI flag.
    """
    if not value or value.startswith("-"):
        return None
    matched = _PATH_CHARS_RE.fullmatch(value)
    return matched.group(0) if matched else None


def _diff_range(compare_branch: str) -> str:
    """Return the validated ``<compare_branch>...HEAD`` merge-base range.

    The three-dot range MUST match the one diff-cover uses internally
    (``GitDiffTool.diff_committed`` defaults to ``<compare_branch>...HEAD``).  A
    two-dot ``<compare_branch> HEAD`` range compares the tips instead, so every
    commit that landed on the base branch after this branch diverged shows up
    as a changed line that diff-cover never measures - inflating
    ``changed_lines`` and failing the gate with a bogus "N unmeasured lines"
    result even though the PR touched no production files at all.

    ``compare_branch`` is validated with ``_validate_ref`` (regex fullmatch,
    rejects a leading ``-``) before it is embedded, so the range cannot carry an
    injection payload or be read as an extra argument.
    """
    safe_ref = _validate_ref(compare_branch, "compare-branch")
    return f"{safe_ref}...HEAD"


def _count_added_lines(diff_range: str, filepath: str) -> int:
    """Count non-blank lines added to *filepath* within *diff_range*.

    Both arguments are regex fullmatch-bounded before they reach
    ``subprocess`` (resolves pythonsecurity:S8705).
    """
    safe_range = _safe_repo_path(diff_range)
    safe_path = _safe_repo_path(filepath)
    if safe_range is None or safe_path is None:
        return 0
    try:
        result = subprocess.run(
            ["git", "diff", _DIFF_FILTER, safe_range, "--", safe_path],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )
    except Exception:
        return 0
    if result.returncode != 0:
        return 0
    return sum(
        1
        for diff_line in result.stdout.splitlines()
        if diff_line.startswith("+") and not diff_line.startswith("+++") and diff_line[1:].strip()
    )


def _get_changed_production_files(compare_branch: str, language: str) -> dict[str, int]:
    """Return {filepath: non_blank_line_count} for changed production files.

    Diffs ``<compare_branch>...HEAD`` (three-dot / merge-base, matching
    diff-cover) to find changed files, then counts non-blank added lines per
    file.  Files matching the exclusion patterns are skipped.  Returns an empty
    dict when the range is invalid or the diff fails.
    """
    pathspecs = _EXTENSIONS.get(language)
    if not pathspecs:
        return {}
    try:
        diff_range = _diff_range(compare_branch)
    except ValueError:
        return {}
    safe_range = _safe_repo_path(diff_range)
    if safe_range is None:
        return {}
    try:
        result = subprocess.run(
            ["git", "diff", _DIFF_FILTER, "--name-only", safe_range, "--", *pathspecs],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}

    files: dict[str, int] = {}
    for line in result.stdout.strip().splitlines():
        filepath = line.strip()
        if not filepath or _is_excluded(filepath):
            continue
        count = _count_added_lines(diff_range, filepath)
        if count > 0:
            files[filepath] = count
    return files


def _normalize_js_report(report_path: Path, src_root: str) -> Path | None:
    """Rewrite relative ``SF:`` entries in an LCOV report to absolute paths.

    vitest's v8 reporter emits source paths relative to the frontend project
    root (e.g. ``src/App.vue``), while diff-cover matches report paths against
    repo-relative git diff paths (``frontend/src/App.vue``).  Left as-is, no
    frontend file ever matches and every changed JS line is reported as
    unmeasured.  Resolving the relative entries against *src_root* (under the
    repo root) makes diff-cover relativise them back to the same repo-relative
    paths as the diff.

    ``report_path`` (a CLI-supplied path) and *src_root* are regex
    fullmatch-bounded before any file operation, and each rewritten ``SF:``
    entry must resolve inside *src_root* so report content can never escape it.

    Returns the path to a normalised copy when anything changed, otherwise
    ``None``.  The caller owns (and deletes) any returned temp file.
    """
    safe_report = Path(_sanitize_path(str(report_path), "js report"))
    safe_src_root = _sanitize_path(src_root, "js-src-root")
    root = (REPO_ROOT / safe_src_root).resolve()
    try:
        lines = safe_report.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    rewritten: list[str] = []
    changed = False
    for line in lines:
        raw = line[len("SF:") :] if line.startswith("SF:") else ""
        if raw and not Path(raw).is_absolute():
            candidate = (root / raw).resolve()
            if candidate.is_relative_to(root):
                rewritten.append(f"SF:{candidate}")
                changed = True
            else:
                rewritten.append(line)
        else:
            rewritten.append(line)
    if not changed:
        return None

    fd, tmp_name = tempfile.mkstemp(prefix="lcov-normalized-", suffix=".info")
    os.close(fd)
    tmp_path = Path(tmp_name)
    # tmp_path is a fresh file created by tempfile.mkstemp (not derived from any
    # input); only the (containment-checked) LCOV text content is written here.
    tmp_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")  # NOSONAR
    return tmp_path


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
                f"[{self.language}] FAIL — {self.actual_pct:.1f}% effective coverage, "
                f"{self.unmeasured_lines} unmeasured line(s) at 0% < {self.threshold}%"
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
    subprocess.run(  # NOSONAR
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )

    if not json_report_path.exists():
        return None
    try:
        return json.loads(json_report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    finally:
        with contextlib.suppress(OSError):
            json_report_path.unlink()


def _production_coverage_from_json(changed_files: dict[str, int], json_data: dict | None) -> tuple[int, int] | None:
    """Return ``(covered_lines, measured_lines)`` over production files only.

    diff-cover's top-level totals cover its *whole* diff - including test files
    the gate excludes - so pairing them with a production-only denominator can
    push coverage above 100%.  Instead, sum the per-file ``covered_lines`` and
    ``violation_lines`` from ``src_stats``, restricted to the production files
    the gate counts.  Each file's measured lines are capped at its changed-line
    count so a report measuring more lines than the gate counted cannot inflate
    the result.

    Returns ``None`` when *json_data* carries no ``src_stats`` mapping.
    """
    if not json_data or not isinstance(json_data.get("src_stats"), dict):
        return None
    src_stats = json_data["src_stats"]
    covered = 0
    measured = 0
    for path, changed in changed_files.items():
        stats = src_stats.get(path)
        if not isinstance(stats, dict):
            continue
        file_covered = len(stats.get("covered_lines") or [])
        file_missing = len(stats.get("violation_lines") or [])
        file_measured = min(file_covered + file_missing, changed)
        covered += min(file_covered, file_measured)
        measured += file_measured
    return covered, measured


def _production_coverage_from_text(output: str, changed_lines: int) -> tuple[float, int, int] | None:
    """Fallback coverage parse from diff-cover's text output.

    Returns ``(effective_pct, measured_lines, unmeasured_lines)`` or ``None``
    when the output carries no coverage line.  The ``Total:`` line gives the
    number of lines diff-cover measured; anything the gate counted beyond that
    is unmeasured and scored at 0%.
    """
    coverage_match = _COVERAGE_LINE_RE.search(output)
    if coverage_match is None:
        return None
    measured_pct_val = float(coverage_match.group(1))
    total_match = _DIFF_COVER_TOTAL_RE.search(output)
    measured_lines = min(int(total_match.group(1)), changed_lines) if total_match else changed_lines
    unmeasured_lines = max(0, changed_lines - measured_lines)
    effective_pct = measured_pct_val * (measured_lines / changed_lines) if changed_lines else 0.0
    return effective_pct, measured_lines, unmeasured_lines


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

    The gate detects unmeasured files by comparing the non-blank changed
    production lines (from ``git diff``) against the production-only lines
    diff-cover measured.  Lines it did not measure count as 0% coverage, and
    the numerator and denominator always come from the same production-only
    file set.
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

    # --- diff-cover found no coverage data for the changed files -> FAIL ---
    # Reaching here means changed_lines > 0 (zero changed production lines
    # already skipped above), so every changed line is unmeasured: 0%.
    if _NO_CHANGED_LINES_RE.search(output):
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

    # --- Extract measured coverage (production files only) ---
    # changed_lines > 0 here: the zero case returned above, so the divisions
    # below are safe.
    measured_pct: float | None = None
    measured_lines = 0
    unmeasured_lines = 0

    production_stats = _production_coverage_from_json(changed_files, json_data)
    if production_stats is not None:
        covered_lines, measured_lines = production_stats
        unmeasured_lines = max(0, changed_lines - measured_lines)
        measured_pct = (covered_lines / changed_lines) * 100.0
    else:
        # Fallback: parse the text output (with the measured-line count so
        # unmeasured lines are still detected when the JSON report is absent).
        text_stats = _production_coverage_from_text(output, changed_lines)
        if text_stats is not None:
            measured_pct, measured_lines, unmeasured_lines = text_stats

    # --- Determine pass/fail ---
    if measured_pct is not None:
        passed = measured_pct >= fail_under
        if not passed:
            reason = f"coverage {measured_pct:.1f}% (with {unmeasured_lines} unmeasured lines at 0%) is below threshold {fail_under}%"
        elif unmeasured_lines > 0:
            reason = (
                f"coverage {measured_pct:.1f}% >= {fail_under}% (but {unmeasured_lines} unmeasured lines counted at 0%)"
            )
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
        with Path(step_summary).open("a") as f:
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
        "--js-src-root",
        default=DEFAULT_JS_SRC_ROOT,
        help=(
            "Directory (relative to the repo root) that the LCOV report's "
            f"relative SF paths resolve against (default: {DEFAULT_JS_SRC_ROOT})."
        ),
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
        python_report = Path(_sanitize_path(str(args.python_report), "python-report"))
    elif default_python.exists():
        python_report = default_python
    else:
        python_report = None

    if args.js_report is not None:
        js_report = Path(_sanitize_path(str(args.js_report), "js-report"))
    elif default_js.exists():
        js_report = default_js
    else:
        js_report = None

    # Normalise the LCOV report so diff-cover can match its paths (see
    # _normalize_js_report).  The temp file, if any, is cleaned up below.
    normalised_js_report: Path | None = None
    if js_report is not None and js_report.exists():
        js_src_root = _sanitize_path(args.js_src_root, "js-src-root")
        normalised = _normalize_js_report(js_report, js_src_root)
        if normalised is not None:
            normalised_js_report = normalised
            js_report = normalised

    results: list[GateResult] = []
    try:
        for language, report in (("Python", python_report), ("JavaScript", js_report)):
            results.append(
                evaluate(
                    language,
                    report,
                    args.compare_branch,
                    args.fail_under,
                    allow_missing=args.allow_missing_reports,
                )
            )
    finally:
        if normalised_js_report is not None:
            with contextlib.suppress(OSError):
                normalised_js_report.unlink()

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
