"""Unit tests for the changed-lines coverage gate (scripts/run_coverage_gate.py).

Tests the pure decision logic of the ``evaluate`` function: skip cases
(missing report, no changed coverable lines), pass, and fail.  The actual
diff-cover invocation is mocked so the tests are fast and offline.
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
# Missing-report skip
# ---------------------------------------------------------------------------
def test_evaluate_missing_report_skips():
    result = mod.evaluate(
        language="Python",
        report_path=None,
        compare_branch="origin/main",
        fail_under=90,
    )
    assert result.skipped is True
    assert result.passed is True
    assert result.actual_pct is None
    assert "no coverage report found" in result.skip_reason.lower()


def test_evaluate_nonexistent_report_file_skips(tmp_path):
    result = mod.evaluate(
        language="JavaScript",
        report_path=tmp_path / "nonexistent" / "lcov.info",
        compare_branch="origin/main",
        fail_under=90,
    )
    assert result.skipped is True
    assert result.passed is True
    assert "no coverage report found" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# No changed coverable lines
# ---------------------------------------------------------------------------
def test_evaluate_no_changed_lines_skips(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    with patch.object(mod, "_run_diff_cover", return_value=(0, "No lines with coverage information in this diff")):
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
# Coverage above threshold → pass
# ---------------------------------------------------------------------------
def test_evaluate_above_threshold_passes(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    output = "Coverage on lines differing from 'origin/main': 95.0%"
    with patch.object(mod, "_run_diff_cover", return_value=(0, output)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is True
    assert result.actual_pct == 95.0


# ---------------------------------------------------------------------------
# Coverage below threshold → fail
# ---------------------------------------------------------------------------
def test_evaluate_below_threshold_fails(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    output = "Coverage on lines differing from 'origin/main': 75.0%\nCoverage threshold not met: 75.0% < 90%"
    with patch.object(mod, "_run_diff_cover", return_value=(1, output)):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is False
    assert result.actual_pct == 75.0


# ---------------------------------------------------------------------------
# Exact boundary (coverage == threshold) → pass
# ---------------------------------------------------------------------------
def test_evaluate_exact_threshold_passes(tmp_path):
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    output = "Coverage on lines differing from 'origin/main': 90.0%"
    with patch.object(mod, "_run_diff_cover", return_value=(0, output)):
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
    m = mod._COVERAGE_LINE_RE.search("Coverage on lines differing from 'origin/main': 87.5%")
    assert m is not None
    assert float(m.group(1)) == 87.5


def test_threshold_not_met_regex_matches():
    assert mod._THRESHOLD_NOT_MET_RE.search("Coverage threshold not met: 75.0% < 90%")


# ---------------------------------------------------------------------------
# COVERAGE_THRESHOLD constant
# ---------------------------------------------------------------------------
def test_default_threshold_is_90():
    assert mod.COVERAGE_THRESHOLD == 90
