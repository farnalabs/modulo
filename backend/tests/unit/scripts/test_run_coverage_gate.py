"""Unit tests for the changed-lines coverage gate (scripts/run_coverage_gate.py).

Tests the pure decision logic of the ``evaluate`` function: fail-closed on
missing reports, skip on no-changed-lines, pass/fail on threshold, tiny-diff
exemption, and unmeasured-file detection.  The actual diff-cover invocation is
mocked so the tests are fast and offline.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_coverage_gate.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_coverage_gate.py)")

_loader = SourceFileLoader("run_coverage_gate", str(script_path))
mod = module_from_spec(spec_from_loader("run_coverage_gate", _loader))
sys.modules[mod.__name__] = mod  # register before exec so @dataclass works
_loader.exec_module(mod)


# ---------------------------------------------------------------------------
# Captured real diff-cover 10.5.1 output (--compare-branch=origin/main).
# These are the *actual* strings the tool emits — the gate must parse these,
# not synthetic text.  The combined form is stdout + "\n" + stderr, which
# is exactly what run_coverage_gate._run_diff_cover returns.
# ---------------------------------------------------------------------------
REAL_DIFF_COVER_PASS_STDOUT = """-------------
Diff Coverage
Diff: origin/main...HEAD, staged and unstaged changes
-------------
src/calc.py (100%): No lines missing
-------------
Total:   1 line
Missing: 0 lines
Coverage: 100%
-------------"""

REAL_DIFF_COVER_PASS_90_STDOUT = """-------------
Diff Coverage
Diff: origin/main...HEAD, staged and unstaged changes
-------------
src/calc.py (90%): No lines missing
-------------
Total:   1 line
Missing: 0 lines
Coverage: 90%
-------------"""

REAL_DIFF_COVER_FAIL_STDOUT = """-------------
Diff Coverage
Diff: origin/main...HEAD, staged and unstaged changes
-------------
src/calc.py (0%): Missing lines 4
-------------
Total:   1 line
Missing: 1 line
Coverage: 0%
-------------"""

REAL_DIFF_COVER_FAIL_STDERR = "Failure. Coverage is below 90%."

REAL_DIFF_COVER_FAIL_COMBINED = REAL_DIFF_COVER_FAIL_STDOUT + "\n" + REAL_DIFF_COVER_FAIL_STDERR

REAL_DIFF_COVER_NO_LINES_STDOUT = """-------------
Diff Coverage
Diff: origin/main...HEAD, staged and unstaged changes
-------------
No lines with coverage information in this diff.
-------------"""

# A JSON report showing measured lines with 100% coverage
JSON_REPORT_PASS = {
    "src_stats": {"src/calc.py": {"percent_covered": 100.0, "covered_lines": [4, 5], "violation_lines": []}},
    "total_num_lines": 2,
    "total_num_violations": 0,
    "total_percent_covered": 100.0,
    "num_changed_lines": 2,
}

# A JSON report showing measured lines with 50% coverage (some unmeasured)
JSON_REPORT_PARTIAL = {
    "src_stats": {"src/calc.py": {"percent_covered": 100.0, "covered_lines": [4, 5], "violation_lines": []}},
    "total_num_lines": 2,
    "total_num_violations": 0,
    "total_percent_covered": 100.0,
    "num_changed_lines": 10,
}

# A JSON report with no measured files (all unmeasured)
JSON_REPORT_EMPTY = {
    "src_stats": {},
    "total_num_lines": 0,
    "total_num_violations": 0,
    "total_percent_covered": 100.0,
    "num_changed_lines": 5,
}


# ---------------------------------------------------------------------------
# Missing report — fail-closed (the default, CI behaviour)
# ---------------------------------------------------------------------------
def test_evaluate_missing_report_fails_closed():
    with patch.object(mod, "_get_changed_production_files", return_value={"src/main.py": 20}):
        result = mod.evaluate(
            language="Python",
            report_path=None,
            compare_branch="origin/main",
            fail_under=90,
        )
    assert result.skipped is False
    assert result.passed is False
    assert result.actual_pct is None
    assert "missing coverage report" in result.skip_reason.lower()


def test_evaluate_nonexistent_report_file_fails_closed(tmp_path):
    with patch.object(mod, "_get_changed_production_files", return_value={"src/main.py": 20}):
        result = mod.evaluate(
            language="JavaScript",
            report_path=tmp_path / "nonexistent" / "lcov.info",
            compare_branch="origin/main",
            fail_under=90,
        )
    assert result.skipped is False
    assert result.passed is False
    assert "missing coverage report" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# Missing report — allow-missing (local convenience)
# ---------------------------------------------------------------------------
def test_evaluate_missing_report_allow_missing_skips():
    with patch.object(mod, "_get_changed_production_files", return_value={"src/main.py": 20}):
        result = mod.evaluate(
            language="Python",
            report_path=None,
            compare_branch="origin/main",
            fail_under=90,
            allow_missing=True,
        )
    assert result.skipped is True
    assert result.passed is True
    assert result.actual_pct is None
    assert "no coverage report found" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# No changed coverable lines — all unmeasured → FAIL (0%)
# ---------------------------------------------------------------------------
def test_evaluate_no_changed_lines_fails(tmp_path):
    """diff-cover says 'no changed coverable lines' but there ARE changed lines
    (they're just unmeasured) → fail with 0%."""
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/main.py": 20}),
        patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_NO_LINES_STDOUT)),
        patch.object(mod, "_get_diff_cover_json", return_value=JSON_REPORT_EMPTY),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is False
    assert result.actual_pct == 0.0
    assert result.unmeasured_lines == 20


# ---------------------------------------------------------------------------
# Coverage above threshold → pass
# ---------------------------------------------------------------------------
def test_evaluate_above_threshold_passes(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    json_pass = {
        "src_stats": {
            "src/calc.py": {
                "percent_covered": 100.0,
                "covered_lines": list(range(50)),
                "violation_lines": [],
            }
        },
        "total_num_lines": 50,
        "total_num_violations": 0,
        "total_percent_covered": 100.0,
        "num_changed_lines": 50,
    }
    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 50}),
        patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)),
        patch.object(mod, "_get_diff_cover_json", return_value=json_pass),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is True
    assert result.actual_pct == 100.0


# ---------------------------------------------------------------------------
# Coverage below threshold → fail
# ---------------------------------------------------------------------------
def test_evaluate_below_threshold_fails(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    json_fail = {
        "src_stats": {
            "src/calc.py": {
                "percent_covered": 0.0,
                "covered_lines": [],
                "violation_lines": [4],
            }
        },
        "total_num_lines": 1,
        "total_num_violations": 1,
        "total_percent_covered": 0.0,
        "num_changed_lines": 50,
    }
    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 50}),
        patch.object(mod, "_run_diff_cover", return_value=(1, REAL_DIFF_COVER_FAIL_COMBINED)),
        patch.object(mod, "_get_diff_cover_json", return_value=json_fail),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is False
    assert result.actual_pct == 0.0
    assert result.unmeasured_lines > 0


# ---------------------------------------------------------------------------
# Exact boundary (coverage == threshold) → pass
# ---------------------------------------------------------------------------
def test_evaluate_exact_threshold_passes(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    json_exact = {
        "src_stats": {
            "src/calc.py": {
                "percent_covered": 100.0,
                "covered_lines": list(range(50)),
                "violation_lines": [],
            }
        },
        "total_num_lines": 50,
        "total_num_violations": 0,
        "total_percent_covered": 100.0,
        "num_changed_lines": 50,
    }
    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 50}),
        patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_90_STDOUT)),
        patch.object(mod, "_get_diff_cover_json", return_value=json_exact),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is True
    assert result.actual_pct is not None


# ---------------------------------------------------------------------------
# Unmeasured file detection
# ---------------------------------------------------------------------------
def test_evaluate_unmeasured_file_fails(tmp_path):
    """A changed file not in the coverage report → unmeasured lines → 0% for those → fail."""
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    # 50 changed lines total, diff-cover only measured 20
    with (
        patch.object(
            mod,
            "_get_changed_production_files",
            return_value={
                "src/calc.py": 20,
                "src/new_module.py": 30,
            },
        ),
        patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)),
        patch.object(
            mod,
            "_get_diff_cover_json",
            return_value={
                "src_stats": {
                    "src/calc.py": {"percent_covered": 100.0, "covered_lines": [1, 2], "violation_lines": []}
                },
                "total_num_lines": 20,
                "total_num_violations": 0,
                "total_percent_covered": 100.0,
                "num_changed_lines": 50,
            },
        ),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is False
    assert result.unmeasured_lines == 30


# ---------------------------------------------------------------------------
# Merge-base diff range
# ---------------------------------------------------------------------------
def test_changed_production_files_uses_merge_base_range():
    """Regression: ``git diff`` must use the three-dot merge-base range.

    diff-cover's ``GitDiffTool.diff_committed`` diffs ``<branch>...HEAD``.  If
    the gate counts changed lines with a two-dot ``<branch> HEAD`` range
    instead, every commit that landed on the base branch after the PR branched
    is counted as a changed line that diff-cover never measures — producing a
    bogus "N unmeasured lines" failure on a PR that touched no production files
    (observed on PR #699: 31 phantom unmeasured JS lines).
    """
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        if "--name-only" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "frontend/src/foo.ts\n", "")
        return subprocess.CompletedProcess(cmd, 0, "+++ b/frontend/src/foo.ts\n+line one\n+line two\n", "")

    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        files = mod._get_changed_production_files("origin/main", "JavaScript")

    assert files == {"frontend/src/foo.ts": 2}
    assert captured, "expected git diff invocations"
    for cmd in captured:
        assert "origin/main...HEAD" in cmd, cmd
        assert "origin/main" not in cmd, cmd


# ---------------------------------------------------------------------------
# Summary string formatting
# ---------------------------------------------------------------------------
def test_summary_skipped():
    result = mod.GateResult(
        language="Python",
        skipped=True,
        skip_reason="no report",
        passed=True,
        actual_pct=None,
        threshold=90,
    )
    summary = result.summary()
    assert "[Python] SKIPPED" in summary
    assert "no report" in summary


def test_summary_pass():
    result = mod.GateResult(
        language="JavaScript",
        skipped=False,
        skip_reason="",
        passed=True,
        actual_pct=92.5,
        threshold=90,
    )
    summary = result.summary()
    assert "[JavaScript] PASS" in summary
    assert "92.5%" in summary


def test_summary_fail():
    result = mod.GateResult(
        language="Python",
        skipped=False,
        skip_reason="",
        passed=False,
        actual_pct=60.0,
        threshold=90,
    )
    summary = result.summary()
    assert "[Python] FAIL" in summary
    assert "60.0%" in summary


# ---------------------------------------------------------------------------
# Regex pattern unit tests
# ---------------------------------------------------------------------------
def test_no_changed_lines_regex_matches():
    assert mod._NO_CHANGED_LINES_RE.search("No lines with coverage information in this diff")


def test_no_changed_lines_regex_case_insensitive():
    assert mod._NO_CHANGED_LINES_RE.search("no LINES with COVERAGE information in this diff")


def test_coverage_line_regex_extracts_value():
    m = mod._COVERAGE_LINE_RE.search("Coverage: 87.5%")
    assert m is not None
    assert float(m.group(1)) == 87.5


def test_threshold_not_met_regex_matches():
    assert mod._THRESHOLD_NOT_MET_RE.search("Failure. Coverage is below 90%.")


# ---------------------------------------------------------------------------
# COVERAGE_THRESHOLD constant
# ---------------------------------------------------------------------------
def test_default_threshold_is_90():
    assert mod.COVERAGE_THRESHOLD == 90


# ---------------------------------------------------------------------------
# Input sanitisation (argument-injection hardening)
# ---------------------------------------------------------------------------
def test_validate_ref_accepts_plain_git_ref():
    assert mod._validate_ref("origin/main", "compare-branch") == "origin/main"
    assert mod._validate_ref("origin/feature/foo-bar", "compare-branch") == "origin/feature/foo-bar"


def test_validate_ref_rejects_flag_injection():
    import pytest

    for bad in ["--fail-under=0", "-x", ""]:
        with pytest.raises(ValueError, match="invalid"):
            mod._validate_ref(bad, "compare-branch")


def test_sanitize_path_accepts_report_paths():
    assert mod._sanitize_path("coverage.xml", "report path") == "coverage.xml"
    assert mod._sanitize_path("../frontend/coverage/lcov.info", "report path") == "../frontend/coverage/lcov.info"


def test_sanitize_path_accepts_windows_paths():
    assert mod._sanitize_path("C:\\Users\\test\\coverage.xml", "report path") == "C:\\Users\\test\\coverage.xml"


def test_sanitize_path_rejects_injection_payload():
    import pytest

    for bad in ["coverage.xml; rm -rf /", "$(curl evil)", "report --extra"]:
        with pytest.raises(ValueError, match=r"invalid|disallowed"):
            mod._sanitize_path(bad, "report path")


# ---------------------------------------------------------------------------
# Tiny-diff exemption
# ---------------------------------------------------------------------------
def test_tiny_diff_exemption_passes(tmp_path):
    """A diff with ≤10 non-blank changed lines passes regardless of coverage."""
    with patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 5}):
        result = mod.evaluate(
            language="Python",
            report_path=tmp_path / "coverage.xml",
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.passed is True
    assert result.tiny_diff is True
    assert result.changed_lines == 5


def test_tiny_diff_at_threshold_passes(tmp_path):
    """Exactly 10 lines is still tiny-diff (at the threshold boundary)."""
    with patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 10}):
        result = mod.evaluate(
            language="Python",
            report_path=tmp_path / "coverage.xml",
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.passed is True
    assert result.tiny_diff is True


def test_just_above_tiny_diff_not_exempt(tmp_path):
    """11 lines is NOT tiny-diff — coverage is checked normally."""
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 11}),
        patch.object(mod, "_run_diff_cover", return_value=(1, REAL_DIFF_COVER_FAIL_COMBINED)),
        patch.object(mod, "_get_diff_cover_json", return_value=JSON_REPORT_EMPTY),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.passed is False
    assert result.tiny_diff is False
    assert result.changed_lines == 11


def test_zero_changed_lines_skips(tmp_path):
    """0 lines → skip (no production changes at all)."""
    with patch.object(mod, "_get_changed_production_files", return_value={}):
        result = mod.evaluate(
            language="Python",
            report_path=tmp_path / "coverage.xml",
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.passed is True
    assert result.tiny_diff is False
    assert result.skipped is True
