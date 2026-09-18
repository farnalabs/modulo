"""Unit tests for the changed-lines coverage gate (scripts/run_coverage_gate.py).

Tests the pure decision logic of the ``evaluate`` function: fail-closed on
missing reports, skip on no-changed-lines, pass/fail on threshold, tiny-diff
exemption, and unmeasured-file detection.  The actual diff-cover invocation is
mocked so the tests are fast and offline.
"""

from __future__ import annotations

import os
import shutil
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
    # src/calc.py is present in the report, so the gate scores it against its
    # coverable changed lines (the single violation) rather than the raw 50
    # non-blank changed lines.  There are no *unmeasured* lines — the one
    # coverable line is simply uncovered.
    assert result.changed_lines == 1
    assert result.measured_lines == 1
    assert result.unmeasured_lines == 0


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

    # 50 changed production lines total, diff-cover only measured 20 of them
    # (all in src/calc.py); src/new_module.py is absent from the report.
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
                    "src/calc.py": {
                        "percent_covered": 100.0,
                        "covered_lines": list(range(1, 21)),
                        "violation_lines": [],
                    }
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
# Non-executable lines in a MEASURED file must not count as unmeasured
# ---------------------------------------------------------------------------
def test_evaluate_measured_file_with_non_executable_lines_passes(tmp_path):
    """A measured file whose executable lines are all covered passes even when
    ``git diff`` counted extra non-executable (annotation/decorator) lines.

    Regression: the gate used to compute ``covered / non_blank_changed_lines``
    and score every line coverage.py cannot measure as 0%, which made a fully
    tested route module fail on its Pydantic/annotation lines.
    """
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    # 120 non-blank changed lines in the diff, but only 100 executable lines
    # in the coverage report, all covered.
    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/routes.py": 120}),
        patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)),
        patch.object(
            mod,
            "_get_diff_cover_json",
            return_value={
                "src_stats": {
                    "src/routes.py": {
                        "percent_covered": 100.0,
                        "covered_lines": list(range(1, 101)),
                        "violation_lines": [],
                    }
                },
                "total_num_lines": 100,
                "num_changed_lines": 120,
                "total_percent_covered": 100.0,
            },
        ),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.passed is True
    assert result.actual_pct == 100.0
    assert result.measured_lines == 100
    assert result.unmeasured_lines == 0


def test_unmeasured_file_lines_counts_only_files_absent_from_report():
    changed = {"src/present.py": 40, "src/absent.py": 30}
    json_data = {"src_stats": {"src/present.py": {"covered_lines": [], "violation_lines": []}}}

    assert mod._unmeasured_file_lines(changed, json_data) == 30


def test_schema_ts_is_excluded_from_the_gate():
    """The generated openapi-typescript file is out of scope (as it is for Sonar)."""
    assert mod._is_excluded("frontend/src/lib/api/schema.ts") is True
    # A hand-written TS file in the same directory is still gated.
    assert mod._is_excluded("frontend/src/lib/api/client.ts") is False


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


# ---------------------------------------------------------------------------
# Production-only coverage (excluded/test-file lines must not inflate it)
# ---------------------------------------------------------------------------
def test_production_coverage_ignores_non_production_files():
    changed = {"frontend/src/App.vue": 4}
    json_data = {
        "src_stats": {
            "frontend/src/App.vue": {"covered_lines": [1, 2], "violation_lines": []},
            "frontend/src/foo.spec.ts": {"covered_lines": [1, 2, 3, 4, 5, 6], "violation_lines": []},
        },
        "total_num_lines": 8,
        "num_changed_lines": 8,
        "total_percent_covered": 100.0,
    }

    assert mod._production_coverage_from_json(changed, json_data) == (2, 2)


def test_production_coverage_caps_measured_at_changed_lines():
    changed = {"src/calc.py": 3}
    json_data = {
        "src_stats": {"src/calc.py": {"covered_lines": [1, 2, 3, 4, 5], "violation_lines": []}},
        "total_num_lines": 5,
        "num_changed_lines": 5,
        "total_percent_covered": 100.0,
    }

    assert mod._production_coverage_from_json(changed, json_data) == (3, 3)


def test_production_coverage_from_text_detects_unmeasured_lines():
    pct, measured, unmeasured = mod._production_coverage_from_text(REAL_DIFF_COVER_PASS_STDOUT, 2)

    assert measured == 1
    assert unmeasured == 1
    assert pct == 50.0


# ---------------------------------------------------------------------------
# LCOV path normalisation (vitest emits paths relative to frontend/)
# ---------------------------------------------------------------------------
def test_normalize_js_report_resolves_relative_paths(tmp_path):
    report = tmp_path / "lcov.info"
    report.write_text("TN:\nSF:src/App.vue\nDA:1,1\nSF:/abs/other.vue\nDA:2,1\nend_of_record\n")

    normalised = mod._normalize_js_report(report, "frontend")

    assert normalised != report
    content = normalised.read_text()
    assert f"SF:{(mod.REPO_ROOT / 'frontend' / 'src/App.vue').resolve()}" in content
    assert "SF:/abs/other.vue" in content
    normalised.unlink()


def test_normalize_js_report_is_noop_for_absolute_paths(tmp_path):
    report = tmp_path / "lcov.info"
    report.write_text("SF:/abs/App.vue\n")

    assert mod._normalize_js_report(report, "frontend") is None


def test_normalize_js_report_does_not_escape_src_root(tmp_path):
    report = tmp_path / "lcov.info"
    report.write_text("SF:../../../etc/passwd\n")

    assert mod._normalize_js_report(report, "frontend") is None


# ---------------------------------------------------------------------------
# Present-file denominator regression (PR #704)
# ---------------------------------------------------------------------------
def test_evaluate_present_file_scores_only_coverable_lines(tmp_path):
    """A file present in the report is scored against its coverable changed
    lines, not the raw non-blank changed-line count.

    Regression: v8 never instruments static ``.vue`` template markup, so a
    component whose executable lines are well covered failed the gate because
    its template lines were counted as unmeasured 0% lines (observed on PR
    #704: 31.2% effective coverage while SonarCloud reported 100% new-code
    coverage).
    """
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    # 100 non-blank changed lines in the diff, but only 20 are coverable and 18
    # of those are covered → 90% effective, not 18%.
    json_present = {
        "src_stats": {
            "frontend/src/components/Foo.vue": {
                "percent_covered": 90.0,
                "covered_lines": list(range(1, 19)),
                "violation_lines": [19, 20],
            }
        },
        "total_num_lines": 20,
        "total_num_violations": 2,
        "total_percent_covered": 90.0,
        "num_changed_lines": 100,
    }
    with (
        patch.object(
            mod,
            "_get_changed_production_files",
            return_value={"frontend/src/components/Foo.vue": 100},
        ),
        patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_90_STDOUT)),
        patch.object(mod, "_get_diff_cover_json", return_value=json_present),
    ):
        result = mod.evaluate(
            language="JavaScript",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.passed is True
    assert result.actual_pct == 90.0
    assert result.changed_lines == 20
    assert result.measured_lines == 20
    assert result.unmeasured_lines == 0


def test_excludes_repo_test_and_generated_paths():
    """Exclusion patterns must cover this repo's real test/generated paths.

    The generic ``tests/**`` globs did not match ``frontend/src/__tests__/**``
    or ``frontend/tests/e2e/**``, so changed spec lines were counted as 0%
    production coverage on PR #704.
    """
    for path in (
        "frontend/src/__tests__/Foo.spec.ts",
        "frontend/tests/e2e/foo.spec.ts",
        "frontend/src/locales/en-US.js",
        "frontend/src/lib/api/schema.ts",
        "backend/tests/unit/scripts/test_run_coverage_gate.py",
    ):
        assert mod._is_excluded(path) is True, path

    assert mod._is_excluded("frontend/src/components/variants/Foo.vue") is False


# ---------------------------------------------------------------------------
# Executable-line detection helpers (FAR-962)
# ---------------------------------------------------------------------------
class TestIsExecutablePythonLine:
    """Unit tests for _is_executable_python_line."""

    def test_blank_line_is_not_executable(self):
        assert mod._is_executable_python_line("") is False
        assert mod._is_executable_python_line("   ") is False

    def test_comment_is_not_executable(self):
        assert mod._is_executable_python_line("# a comment") is False
        assert mod._is_executable_python_line("  # indented comment") is False

    def test_assignment_is_executable(self):
        assert mod._is_executable_python_line("x = 1") is True
        assert mod._is_executable_python_line("x: int = 1") is True

    def test_control_flow_is_executable(self):
        assert mod._is_executable_python_line("if x:") is True
        assert mod._is_executable_python_line("for i in range(10):") is True
        assert mod._is_executable_python_line("while True:") is True

    def test_function_def_is_executable(self):
        assert mod._is_executable_python_line("def foo():") is True

    def test_class_def_is_executable(self):
        assert mod._is_executable_python_line("class Foo:") is True

    def test_import_is_executable(self):
        assert mod._is_executable_python_line("import os") is True
        assert mod._is_executable_python_line("from pathlib import Path") is True

    def test_expression_is_executable(self):
        assert mod._is_executable_python_line("print('hello')") is True
        assert mod._is_executable_python_line("foo.bar()") is True

    def test_string_literal_is_not_executable(self):
        """A standalone string literal is not instrumented by coverage.py."""
        assert mod._is_executable_python_line("'just a string'") is False
        assert mod._is_executable_python_line('"""docstring"""') is False
        assert mod._is_executable_python_line("'''triple'''") is False

    def test_string_in_assignment_is_executable(self):
        """A string inside an assignment is executable (the assignment is)."""
        assert mod._is_executable_python_line('x = "hello"') is True
        assert mod._is_executable_python_line("y = '''multi'''") is True

    def test_decorator_is_executable(self):
        assert mod._is_executable_python_line("@decorator") is True

    def test_return_statement_is_executable(self):
        assert mod._is_executable_python_line("return x") is True

    def test_raise_statement_is_executable(self):
        assert mod._is_executable_python_line("raise ValueError()") is True


class TestIsExecutableJsLine:
    """Unit tests for _is_executable_js_line."""

    def test_blank_line_is_not_executable(self):
        assert mod._is_executable_js_line("") is False
        assert mod._is_executable_js_line("   ") is False

    def test_single_line_comment_is_not_executable(self):
        assert mod._is_executable_js_line("// a comment") is False
        assert mod._is_executable_js_line("  // indented comment") is False

    def test_trailing_comment_stripped(self):
        """A line with code + trailing comment counts as executable."""
        assert mod._is_executable_js_line("x = 1; // comment") is True

    def test_code_is_executable(self):
        assert mod._is_executable_js_line("const x = 1;") is True
        assert mod._is_executable_js_line("if (x) {") is True
        assert mod._is_executable_js_line("return y;") is True


# ---------------------------------------------------------------------------
# FAR-962 regression: deleted and non-executable lines excluded from denominator
# ---------------------------------------------------------------------------
def test_deletion_only_diff_skips():
    """A PR that only deletes lines (no executable additions) → SKIP.

    Regression: deleted lines were entering the changed-lines denominator,
    causing deletion-heavy PRs to fail with inflated 'unmeasured lines'
    counts (observed on PR #704: 31.2% effective coverage from deleted
    lines counted as unmeasured).
    """
    with patch.object(mod, "_get_changed_production_files", return_value={}):
        result = mod.evaluate(
            language="Python",
            report_path=Path("coverage.xml"),
            compare_branch="origin/main",
            fail_under=90,
        )
    assert result.skipped is True
    assert result.passed is True


def test_comment_only_diff_skips():
    """A PR that only adds comments/docstrings → SKIP (no executable lines).

    Regression: a diff with no executable changed lines previously FAILed
    with 0.0% coverage because comment/docstring lines entered the
    denominator as 'unmeasured' (observed on PR #710: 0.0% coverage, 11
    unmeasured lines from comment-only changes).

    Before fix: _count_added_lines counted all non-blank added lines,
    including comments and docstrings.  A comment-only PR with 11 comment
    lines had changed_lines=11, and diff-cover found no coverage data for
    them → 0.0% effective coverage → FAIL.

    After fix: _count_added_lines uses ast (Python) or regex (JS) to count
    only executable lines.  A comment-only PR has changed_lines=0 → SKIP.
    """
    # Simulate a file where every added line is a comment or docstring.
    # _count_added_lines now returns 0 for such a file, so
    # _get_changed_production_files returns {}.
    with patch.object(mod, "_get_changed_production_files", return_value={}):
        result = mod.evaluate(
            language="Python",
            report_path=Path("coverage.xml"),
            compare_branch="origin/main",
            fail_under=90,
        )
    assert result.skipped is True
    assert result.passed is True


def test_added_uncovered_executable_lines_fails(tmp_path):
    """A PR that adds genuinely uncovered executable lines → FAIL.

    This is the case the gate MUST still catch: new executable code with no
    coverage must fail below the 90% threshold.
    """
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    # 50 executable changed lines, none covered (file absent from report).
    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/new_module.py": 50}),
        patch.object(mod, "_run_diff_cover", return_value=(1, REAL_DIFF_COVER_FAIL_COMBINED)),
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
    assert result.unmeasured_lines == 50


def test_comment_only_diff_produces_zero_executable_lines():
    """Verify _count_added_lines returns 0 when all added lines are comments.

    This directly proves the fix: before FAR-962, _count_added_lines would
    return the count of comment lines (non-blank), inflating the denominator.
    After the fix, it returns 0 because no line is executable.
    """
    # Python: comment-only diff
    py_diff = """+++ b/src/module.py
+# This is a comment
+    # Indented comment
+\"\"\"A docstring.\"\"\"
+"""
    with patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=py_diff, stderr="")
        count = mod._count_added_lines("origin/main...HEAD", "src/module.py")
    assert count == 0

    # JS: comment-only diff
    js_diff = """+++ b/src/app.ts
+// This is a comment
+  // Indented comment
+"""
    with patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=js_diff, stderr="")
        count = mod._count_added_lines("origin/main...HEAD", "src/app.ts")
    assert count == 0


def test_deletion_lines_not_counted():
    """Verify deleted lines do not enter the changed-lines count.

    _count_added_lines only counts lines starting with '+', so deletions
    (lines starting with '-') are never counted.
    """
    mixed_diff = """+++ b/src/module.py
+added_line_1
+added_line_2
-deleted_line_1
-deleted_line_2
 context_line
"""
    with patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=mixed_diff, stderr="")
        count = mod._count_added_lines("origin/main...HEAD", "src/module.py")
    assert count == 2  # only the two added lines


def test_multiline_docstring_body_lines_not_counted():
    """Body lines inside a multi-line docstring are data, not statements.

    A single-line predicate cannot see the enclosing triple quotes, so body
    lines fail ``ast.parse`` and hit the lenient fallback.  The stateful
    iterator must exclude the docstring opening line, its body, and its
    closing delimiter — only ``def f():`` and ``return 1`` are executable.
    """
    py_diff = """+++ b/src/module.py
+def f():
+    \"\"\"Summary.
+
+    Body line one.
+    Body line two.
+    \"\"\"
+    return 1
"""
    with patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=py_diff, stderr="")
        count = mod._count_added_lines("origin/main...HEAD", "src/module.py")
    assert count == 2


def test_multiline_docstring_assignment_still_counted():
    """An assignment whose value opens a multi-line string is executable.

    The ``x = ...`` prefix is real code, so the opening line counts; the
    string body and closing delimiter that follow do not.
    """
    py_diff = """+++ b/src/module.py
+x = \"\"\"
+line one
+line two
+\"\"\"
+y = 2
"""
    with patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=py_diff, stderr="")
        count = mod._count_added_lines("origin/main...HEAD", "src/module.py")
    assert count == 2  # x = """ and y = 2


def test_js_block_comment_body_lines_not_counted():
    """JSDoc ``/* ... */`` continuation lines are not executable."""
    js_diff = """+++ b/src/app.ts
+/**
+ * JSDoc summary.
+ * @param x - input
+ */
+export const x = 1;
"""
    with patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=js_diff, stderr="")
        count = mod._count_added_lines("origin/main...HEAD", "src/app.ts")
    assert count == 1


def test_js_inline_block_comment_only_not_counted():
    """A complete inline ``/* ... */`` comment line carries no code."""
    js_diff = """+++ b/src/app.ts
+/* inline */
+const y = 2; /* trailing */
"""
    with patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=js_diff, stderr="")
        count = mod._count_added_lines("origin/main...HEAD", "src/app.ts")
    assert count == 1


# ---------------------------------------------------------------------------
# FAR-962 end-to-end: real git diff through the unpatched code path
# ---------------------------------------------------------------------------
_GIT = shutil.which("git") or "git"


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(  # noqa: S603 — trusted fixed git args, test helper
        [_GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_repo(repo: Path, base_content: str = "def f():\n    return 1\n") -> dict[str, str]:
    """Create a git repo with a ``main`` commit; return an env for commits."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    (repo / "src").mkdir(parents=True)
    (repo / "src" / "module.py").write_text(base_content, encoding="utf-8")
    _git(repo, env, "init", "-b", "main")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-m", "base")
    return env


def _commit_on_feature(repo: Path, env: dict[str, str]) -> None:
    _git(repo, env, "checkout", "-b", "feature")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-m", "feature")


def test_evaluate_comment_only_diff_skips_via_real_git_diff(tmp_path):
    """Drive a real comment-only diff through the unpatched code path.

    Stronger than the mocked tests: ``_get_changed_production_files`` and
    ``_count_added_lines`` both run real ``git diff`` against a scratch repo,
    proving the executable-line filter yields zero changed lines end-to-end.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _init_repo(repo)
    # 12 added comment lines: above the tiny-diff exemption, so a pre-fix
    # count of these lines would flow into the missing-report path and FAIL
    # rather than being masked by the ≤10-line exemption.
    comments = "".join(f"    # comment {i}\n" for i in range(12))
    (repo / "src" / "module.py").write_text(
        f"def f():\n{comments}    return 1\n",
        encoding="utf-8",
    )
    _commit_on_feature(repo, env)

    with patch.object(mod, "REPO_ROOT", repo):
        changed = mod._get_changed_production_files("main", "Python")
        result = mod.evaluate(
            language="Python",
            report_path=None,
            compare_branch="main",
            fail_under=90,
        )

    assert changed == {}
    assert result.skipped is True
    assert result.passed is True


def test_evaluate_docstring_only_diff_skips_via_real_git_diff(tmp_path):
    """A real multi-line-docstring-only diff yields zero executable lines.

    Exercises the triple-quote state tracker end-to-end (real ``git diff``,
    unpatched ``_get_changed_production_files``/``_count_added_lines``).  The
    docstring body lines are not independently parseable, so without state
    tracking they would count as executable and fail the gate.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _init_repo(repo)
    body = "".join(f"    doc line {i}\n" for i in range(12))
    (repo / "src" / "module.py").write_text(
        f'def f():\n    """Summary.\n{body}    """\n    return 1\n',
        encoding="utf-8",
    )
    _commit_on_feature(repo, env)

    with patch.object(mod, "REPO_ROOT", repo):
        changed = mod._get_changed_production_files("main", "Python")
        result = mod.evaluate(
            language="Python",
            report_path=None,
            compare_branch="main",
            fail_under=90,
        )

    assert changed == {}
    assert result.skipped is True
    assert result.passed is True


def test_evaluate_deletion_only_diff_skips_via_real_git_diff(tmp_path):
    """A real deletion-only diff must not enter the changed-lines denominator."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _init_repo(repo, "def f():\n    x = 1\n    return 1\n")
    (repo / "src" / "module.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _commit_on_feature(repo, env)

    with patch.object(mod, "REPO_ROOT", repo):
        changed = mod._get_changed_production_files("main", "Python")
        result = mod.evaluate(
            language="Python",
            report_path=None,
            compare_branch="main",
            fail_under=90,
        )

    assert changed == {}
    assert result.skipped is True
    assert result.passed is True
