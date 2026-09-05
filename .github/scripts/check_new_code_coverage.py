#!/usr/bin/env python3
"""New-code coverage gate — >=90% line coverage on changed production files.

SonarCloud's free plan cannot host custom quality gates ("Organization ... is
not allowed to modify Quality gates"), so the Duncan-approved new-code
coverage >=90% policy is enforced here as a CI check instead. The gate:

1. Computes the PR diff (merge-base vs HEAD) and keeps changed production
    files: backend/src/**/*.py and frontend/src/**, minus the coverage-
    denominator exclusions (migrations, backend/scripts, backend/tools, frontend
    __tests__ and tests, auto-generated schema.ts, type-only paths and *.d.ts).
2. Reads line coverage for those files from the backend cobertura XML
   (pytest-cov) and the frontend lcov report (vitest v8 provider). A changed
   production file absent from its report counts as 0% covered with
   valid lines = its non-blank line count on disk — closing the
   brand-new-untested-file hole. A missing report exempts that language,
   with a loud warning (never a silent pass).
3. Aggregates per language (sum covered / sum valid) and exits 1 if either
   language is below --threshold. Diffs with fewer than --min-lines total
   changed source lines are exempt ("trivial diff").

Branch (condition) coverage from the backend report is aggregated and
reported but NOT enforced (Wave 6).

Exit codes: 0 = pass or trivial diff, 1 = gate failure, 2 = wiring failure
(git error, unreadable/malformed report).

Stdlib only: runs from the repo root in CI with plain python3.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "origin/main"
DEFAULT_COVERAGE_XML = "backend/coverage.xml"
DEFAULT_LCOV = "frontend/coverage/lcov.info"
DEFAULT_THRESHOLD = 90.0
DEFAULT_MIN_LINES = 10

BACKEND_INCLUDE = "backend/src/**/*.py"
FRONTEND_INCLUDE = "frontend/src/**"

# Coverage-denominator exclusions: files that carry no unit-testable production
# logic (or are auto-generated / type-only) must not drag the gate down. This
# list is curated INDEPENDENTLY of SonarCloud's sonar.exclusions and is
# intentionally broader — sonar.exclusions is only "**/schema.ts,**/locales/**,
# backend/tests/**" (it does not cover backend migrations/scripts/tools or the
# frontend __tests__/type-barrel/*.d.ts paths). Critically it MUST include the
# generated frontend/src/lib/api/schema.ts: schema-freshness commits land it on
# every API-touching PR and it has no executable lines, so without this
# exclusion the unmeasured-file fallback would count it as 0% over ~36k lines
# and fail the gate. Regression pin: test_main_type_only_changed_file_absent_from_lcov_passes.
COVERAGE_EXCLUSION_PATTERNS = (
    "backend/src/modulo/db/migrations/**",
    "backend/scripts/**",
    "backend/tools/**",
    "frontend/src/__tests__/**",
    "frontend/tests/**",
    "*schema.ts",
    "frontend/src/types/**",
    "**/*.d.ts",
    "frontend/src/**/*types.ts",
)

# cobertura condition-coverage attribute format: "50% (1/2)".
_CONDITION_RE = re.compile(r"\((\d+)\s*/\s*(\d+)\)")


class GitError(Exception):
    """A git command failed — the gate cannot determine the diff."""


@dataclass
class FileCoverage:
    """Per-file coverage counters from a report (lcov/cobertura)."""

    lines_valid: int = 0
    lines_covered: int = 0
    conditions_valid: int = 0
    conditions_covered: int = 0


@dataclass(frozen=True)
class GateRow:
    """One changed production file's contribution to the gate."""

    path: str
    language: str
    lines_valid: int
    lines_covered: int
    source: str  # "report" | "unmeasured"


# ---------------------------------------------------------------------------
# git diff (thin, overridable for unit tests)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise GitError("git executable not found on PATH")
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell, path from shutil.which
        [git, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise GitError(f"git {args[0]} failed (exit {completed.returncode}): {detail}")
    return completed.stdout


def get_merge_base(base: str, cwd: Path) -> str:
    """Return the merge-base commit of HEAD and the base ref."""
    return _run_git(["merge-base", "HEAD", base], cwd).strip()


def get_changed_files(merge_base: str, cwd: Path) -> list[str]:
    """Return repo-root-relative paths changed between merge_base and HEAD.

    Uses --name-only so the output is pure paths (no rename/status letters).
    """
    out = _run_git(["diff", "--name-only", f"{merge_base}..HEAD"], cwd)
    return [line.strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# path/pattern helpers
# ---------------------------------------------------------------------------


def pattern_matches(pattern: str, path: str) -> bool:
    """Match a repo-relative glob pattern with ``**`` support.

    ``dir/**`` matches everything under dir; ``dir/**/*.ext`` matches .ext
    files at any depth under dir (including directly in dir).
    """
    pattern = pattern.replace("\\", "/").rstrip("/")
    path = path.replace("\\", "/")
    if "**" not in pattern:
        return fnmatch.fnmatch(path, pattern)
    head, _, tail = pattern.partition("**")
    if not path.startswith(head):
        return False
    rest = path[len(head) :]
    tail = tail.lstrip("/")
    if not tail:
        return bool(rest)
    return fnmatch.fnmatch(rest, tail)


def is_coverage_excluded(path: str) -> bool:
    """True when the path is excluded from the coverage denominator."""
    return any(pattern_matches(pattern, path) for pattern in COVERAGE_EXCLUSION_PATTERNS)


def filter_changed_production_files(paths: list[str]) -> list[str]:
    """Keep only changed production source files, minus coverage exclusions."""
    return [
        path
        for path in paths
        if (pattern_matches(BACKEND_INCLUDE, path) or pattern_matches(FRONTEND_INCLUDE, path))
        and not is_coverage_excluded(path)
    ]


def _clean_report_path(raw: str) -> str:
    raw = raw.replace("\\", "/").strip()
    return raw.removeprefix("./")


def _absolute_to_repo_relative(candidate: Path, repo_root: Path) -> str:
    """Best-effort relativization of an absolute report path against repo root."""
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        parts = candidate.as_posix().split("/")
        if "src" in parts:
            index = parts.index("src")
            return "/".join(parts[index - 1 :])
        return candidate.as_posix()


def normalize_report_path(raw: str, repo_root: Path, repo_prefix: str) -> str:
    """Normalize a report path to repo-root-relative POSIX form.

    Reports use source-root-relative paths (``src/modulo/x.py`` from
    pytest-cobertura, ``src/stores/x.ts`` from vitest lcov). Coverage.py may
    emit absolute paths depending on configuration — both are handled.
    """
    raw = _clean_report_path(raw)
    candidate = Path(raw)
    if candidate.is_absolute():
        raw = _absolute_to_repo_relative(candidate, repo_root)
    if raw.startswith(f"{repo_prefix}/"):
        return raw
    if raw.startswith("src/"):
        return f"{repo_prefix}/{raw}"
    return raw


# ---------------------------------------------------------------------------
# report parsing
# ---------------------------------------------------------------------------


def parse_cobertura(path: Path, repo_root: Path) -> dict[str, FileCoverage]:
    """Parse a pytest-cov cobertura XML report into per-file coverage.

    Each <class filename="src/modulo/..."> carries per-line <line> children;
    a line's condition-coverage="50% (1/2)" contributes branch totals, which
    are reported but not enforced (Wave 6).
    """
    # S314: the cobertura XML is a CI artifact generated by our own pytest-cov
    # run (not untrusted input), and this script must stay stdlib-only — it is
    # invoked in CI with the runner's plain python3, outside any project venv.
    root = ET.parse(path).getroot()  # noqa: S314 — trusted CI artifact, stdlib-only constraint
    files: dict[str, FileCoverage] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename")
        if not filename:
            continue
        name = normalize_report_path(filename, repo_root, "backend")
        entry = files.setdefault(name, FileCoverage())
        lines_node = cls.find("lines")
        if lines_node is None:
            continue
        for line in lines_node.findall("line"):
            entry.lines_valid += 1
            if int(line.get("hits", "0")) > 0:
                entry.lines_covered += 1
            condition = line.get("condition-coverage")
            if condition:
                match = _CONDITION_RE.search(condition)
                if match:
                    entry.conditions_covered += int(match.group(1))
                    entry.conditions_valid += int(match.group(2))
    return files


def parse_lcov(path: Path, repo_root: Path) -> dict[str, FileCoverage]:
    """Parse a lcov report (SF:/LF:/LH: records) into per-file coverage."""
    files: dict[str, FileCoverage] = {}
    current: FileCoverage | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("SF:"):
            name = normalize_report_path(line[3:], repo_root, "frontend")
            current = files.setdefault(name, FileCoverage())
        elif line.startswith("LF:") and current is not None:
            current.lines_valid += int(line[3:])
        elif line.startswith("LH:") and current is not None:
            current.lines_covered += int(line[3:])
        elif line == "end_of_record":
            current = None
    return files


def count_non_blank_lines(path: Path) -> int:
    """Count non-blank lines of a file on disk (unmeasured-file denominator)."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        return sum(1 for line in handle if line.strip())


def collect_rows(
    production_files: list[str],
    backend_report: dict[str, FileCoverage] | None,
    frontend_report: dict[str, FileCoverage] | None,
    repo_root: Path,
) -> list[GateRow]:
    """Build gate rows for changed production files.

    Files present in their language's report use the reported counts; files
    absent from an existing report are counted as 0% covered with valid
    lines = non-blank line count on disk. A language with no report at all
    is exempt (warned in main) and contributes no rows.
    """
    rows: list[GateRow] = []
    for path in production_files:
        if pattern_matches(BACKEND_INCLUDE, path):
            language = "backend"
            report = backend_report
        elif pattern_matches(FRONTEND_INCLUDE, path):
            language = "frontend"
            report = frontend_report
        else:
            continue
        if report is None:
            continue
        stats = report.get(path)
        if stats is not None:
            rows.append(GateRow(path, language, stats.lines_valid, stats.lines_covered, "report"))
            continue
        on_disk = repo_root / path
        if not on_disk.is_file():
            print(f"  (skip) changed file not present in tree: {path}")
            continue
        valid = count_non_blank_lines(on_disk)
        print(f"  (unmeasured) {path}: absent from coverage report — counted 0% over {valid} lines")
        rows.append(GateRow(path, language, valid, 0, "unmeasured"))
    return rows


def branch_totals(report: dict[str, FileCoverage], measured_paths: list[str]) -> tuple[int, int]:
    """Aggregate backend branch counters over measured (report-sourced) files."""
    valid = sum(report[p].conditions_valid for p in measured_paths if p in report)
    covered = sum(report[p].conditions_covered for p in measured_paths if p in report)
    return valid, covered


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------


def format_pct(covered: int, valid: int) -> str:
    if valid <= 0:
        return "n/a"
    return f"{covered / valid * 100:.1f}%"


def render_table(rows: list[GateRow]) -> str:
    """Render the per-file summary as a GitHub-flavoured markdown table."""
    lines = [
        "| File | Language | Valid lines | Covered | Coverage | Source |",
        "|---|---|---:|---:|---:|---|",
    ]
    lines.extend(
        f"| {row.path} | {row.language} | {row.lines_valid} | {row.lines_covered} "
        f"| {format_pct(row.lines_covered, row.lines_valid)} | {row.source} |"
        for row in rows
    )
    return "\n".join(lines) + "\n"


def emit_table(rows: list[GateRow]) -> None:
    """Write the per-file table to $GITHUB_STEP_SUMMARY when present, else stdout."""
    table = render_table(rows)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(table)
    else:
        print(table, end="")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enforce new-code line coverage on changed production files.")
    parser.add_argument("--base", default=DEFAULT_BASE, help="Base ref for the PR diff.")
    parser.add_argument("--coverage-xml", default=DEFAULT_COVERAGE_XML, help="Backend cobertura XML path.")
    parser.add_argument("--lcov", default=DEFAULT_LCOV, help="Frontend lcov report path.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Minimum coverage %%.")
    parser.add_argument("--min-lines", type=int, default=DEFAULT_MIN_LINES, help="Trivial-diff exemption floor.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path.cwd()

    try:
        merge_base = get_merge_base(args.base, repo_root)
        changed = get_changed_files(merge_base, repo_root)
    except GitError as exc:
        print(f"::error::New-code coverage gate: git failure: {exc}")
        return 2

    production_files = filter_changed_production_files(changed)
    if not production_files:
        print("New-code coverage gate: no changed production files (backend/src, frontend/src) — nothing to gate.")
        return 0

    backend_report: dict[str, FileCoverage] | None = None
    frontend_report: dict[str, FileCoverage] | None = None
    try:
        backend_xml = repo_root / args.coverage_xml
        if backend_xml.is_file():
            backend_report = parse_cobertura(backend_xml, repo_root)
        else:
            print(
                f"::warning::Backend coverage report not found: {args.coverage_xml} — "
                "backend changed files are EXEMPT this run (warned, not silently passed)."
            )
        frontend_lcov = repo_root / args.lcov
        if frontend_lcov.is_file():
            frontend_report = parse_lcov(frontend_lcov, repo_root)
        else:
            print(
                f"::warning::Frontend lcov report not found: {args.lcov} — "
                "frontend changed files are EXEMPT this run (warned, not silently passed)."
            )
        rows = collect_rows(production_files, backend_report, frontend_report, repo_root)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"::error::New-code coverage gate: could not read coverage reports: {exc}")
        return 2

    emit_table(rows)

    total_valid = sum(row.lines_valid for row in rows)
    if total_valid < args.min_lines:
        print(f"New-code coverage gate: trivial diff ({total_valid} changed source lines < {args.min_lines}) — exempt.")
        return 0

    verdicts: dict[str, str] = {}
    failed_languages: list[str] = []
    for language, report in (("backend", backend_report), ("frontend", frontend_report)):
        language_rows = [row for row in rows if row.language == language]
        valid = sum(row.lines_valid for row in language_rows)
        covered = sum(row.lines_covered for row in language_rows)
        verdicts[language] = "exempt (no report)" if report is None else format_pct(covered, valid)
        if report is not None and valid > 0 and covered / valid * 100 < args.threshold:
            failed_languages.append(language)

    if backend_report is not None:
        measured_backend = [row.path for row in rows if row.language == "backend" and row.source == "report"]
        conditions_valid, conditions_covered = branch_totals(backend_report, measured_backend)
        if conditions_valid:
            print(
                f"Backend branch coverage (reported, not enforced): {conditions_covered}/{conditions_valid} "
                f"({format_pct(conditions_covered, conditions_valid)})"
            )

    if failed_languages:
        print(
            f"::error::New-code coverage gate failed: backend {verdicts['backend']} / "
            f"frontend {verdicts['frontend']} (threshold {args.threshold:g}%)"
        )
        for language in failed_languages:
            print(f"  below threshold: {language}")
        return 1

    print(
        f"New-code coverage gate passed: backend {verdicts['backend']} / "
        f"frontend {verdicts['frontend']} (threshold {args.threshold:g}%)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
