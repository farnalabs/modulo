"""Unit tests for the changed-lines coverage gate (scripts/run_coverage_gate.py).

Tests the pure decision logic of the ``evaluate`` function: fail-closed on
missing reports, skip on no-changed-lines, pass/fail on threshold.  The
actual diff-cover invocation is mocked so the tests are fast and offline.
"""

from __future__ import annotations

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
# not synthetic text.  The combined form is stdout + "\n" + stderr, which is
# exactly what run_coverage_gate._run_diff_cover returns.
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


# ---------------------------------------------------------------------------
# Missing report — fail-closed (the default, CI behaviour)
# ---------------------------------------------------------------------------
def test_evaluate_missing_report_fails_closed():
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


def test_evaluate_nonexistent_report_allow_missing_skips(tmp_path):
    result = mod.evaluate(
        language="JavaScript",
        report_path=tmp_path / "nonexistent" / "lcov.info",
        compare_branch="origin/main",
        fail_under=90,
        allow_missing=True,
    )
    assert result.skipped is True
    assert result.passed is True
    assert "no coverage report found" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# No changed coverable lines — still skips
# ---------------------------------------------------------------------------
def test_evaluate_no_changed_lines_skips(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    with patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_NO_LINES_STDOUT)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is True
    assert result.passed is True
    assert "no changed coverable lines" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# Coverage above threshold → pass (real diff-cover 10.5.1 stdout)
# ---------------------------------------------------------------------------
def test_evaluate_above_threshold_passes(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    with patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)):
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
# Coverage below threshold → fail (real diff-cover 10.5.1 combined output)
# ---------------------------------------------------------------------------
def test_evaluate_below_threshold_fails(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    with patch.object(mod, "_run_diff_cover", return_value=(1, REAL_DIFF_COVER_FAIL_COMBINED)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is False
    assert result.actual_pct == 0.0
    assert "below" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# Exact boundary (coverage == threshold) → pass
# ---------------------------------------------------------------------------
def test_evaluate_exact_threshold_passes(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    with patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_90_STDOUT)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is True
    assert result.actual_pct == 90.0


# ---------------------------------------------------------------------------
# Round-trip against captured REAL diff-cover 10.5.1 output.
# These must fail against the old regexes (which matched synthetic strings diff-cover
# never emits) and pass against the corrected ones — proving the gate actually parses
# real tool output instead of passing only on invented text.
# ---------------------------------------------------------------------------
def test_real_output_pass_parses_coverage_and_passes(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")
    with patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )
    assert result.skipped is False
    assert result.passed is True
    assert result.actual_pct == 100.0


def test_real_output_fail_parses_coverage_and_fails(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")
    with patch.object(mod, "_run_diff_cover", return_value=(1, REAL_DIFF_COVER_FAIL_COMBINED)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )
    assert result.skipped is False
    assert result.passed is False
    assert result.actual_pct == 0.0
    assert "below" in result.skip_reason.lower()


def test_real_output_no_changed_lines_skips(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")
    with patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_NO_LINES_STDOUT)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )
    assert result.skipped is True
    assert result.passed is True
    assert "no changed coverable lines" in result.skip_reason.lower()


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
    # A value that could be read as an extra diff-cover flag must be refused.
    import pytest

    for bad in ["--fail-under=0", "-x", ""]:
        with pytest.raises(ValueError, match="invalid"):
            mod._validate_ref(bad, "compare-branch")


def test_sanitize_path_accepts_report_paths():
    assert mod._sanitize_path("coverage.xml", "report path") == "coverage.xml"
    assert mod._sanitize_path("../frontend/coverage/lcov.info", "report path") == "../frontend/coverage/lcov.info"


def test_sanitize_path_rejects_injection_payload():
    import pytest

    for bad in ["coverage.xml; rm -rf /", "$(curl evil)", "report --extra"]:
        with pytest.raises(ValueError, match=r"invalid|disallowed"):
            mod._sanitize_path(bad, "report path")
