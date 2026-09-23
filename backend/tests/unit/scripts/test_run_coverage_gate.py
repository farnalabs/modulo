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

_REPO_ROOT = script_path.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from scripts.git_hook_env import GIT_HOOK_CONTEXT_VARS  # noqa: E402

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
# diff-cover exits 0 with unparseable output → FAIL closed (FAR-992)
# ---------------------------------------------------------------------------
def test_evaluate_unparseable_output_fails_closed(tmp_path):
    """diff-cover exits 0 but emits no parseable coverage line → fail closed.

    Regression for the latent false-pass: an rc=0 whose output carries neither
    a ``Coverage: <pct>%`` line nor the 'no changed lines' marker silently
    passed the gate.  It must now fail with a distinct reason.
    """
    fake_report = tmp_path / "coverage.xml"
    fake_report.write_text("<coverage/>")

    unparseable = "-------------\nDiff Coverage\nDiff: origin/main...HEAD, staged and unstaged changes\n-------------\n"

    with (
        patch.object(mod, "_get_changed_production_files", return_value={"src/main.py": 20}),
        patch.object(mod, "_run_diff_cover", return_value=(0, unparseable)),
        patch.object(mod, "_get_diff_cover_json", return_value=None),
    ):
        result = mod.evaluate(
            language="Python",
            report_path=fake_report,
            compare_branch="origin/main",
            fail_under=90,
        )

    assert result.skipped is False
    assert result.passed is False
    assert result.actual_pct is None
    assert "no parseable coverage result" in result.skip_reason.lower()


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


def test_unmeasured_file_lines_ignores_files_present_in_the_report():
    """A file absent from diff-cover's ``src_stats`` but present in the raw
    report has no coverable changed lines (all template/import), so it must not
    be scored as unmeasured 0%."""
    changed = {"src/present.py": 40, "src/only_template.py": 3, "src/absent.py": 30}
    json_data = {"src_stats": {"src/present.py": {"covered_lines": [], "violation_lines": []}}}

    assert mod._unmeasured_file_lines(changed, json_data, {"src/only_template.py"}) == 30


def test_report_counts_files_maps_lcov_entries_to_changed_files(tmp_path):
    """``_report_counts_files`` re-keys raw LCOV ``SF:`` paths onto the
    repo-relative changed-file namespace (vitest emits them relative to the
    frontend project root)."""
    component = mod.REPO_ROOT / "frontend/src/components/shared/Foo.vue"
    view = mod.REPO_ROOT / "frontend/src/views/BarView.vue"
    report = tmp_path / "lcov.info"
    report.write_text(f"SF:{component}\nSF:{view}\n")

    changed = {
        "frontend/src/components/shared/Foo.vue": 60,
        "frontend/src/views/BarView.vue": 3,
        "frontend/src/views/AbsentView.vue": 5,
    }

    assert mod._report_counts_files(changed, report, "JavaScript") == {
        "frontend/src/components/shared/Foo.vue",
        "frontend/src/views/BarView.vue",
    }


def test_evaluate_non_instrumentable_view_change_is_not_unmeasured(tmp_path):
    """Regression (FAR-706): a view whose only changed lines are its template
    usage and import (v8 instruments neither) must not fail the gate.

    Observed on PR #913: ``RunDetailView.vue`` gained a 3-line
    ``<KnownFixesPanel>`` wiring with no instrumentable lines, so diff-cover
    omitted it from ``src_stats`` and the gate charged all 3 lines as
    unmeasured 0%, pulling a fully-covered component down to 75%.
    """
    component = mod.REPO_ROOT / "frontend/src/components/shared/KnownFixesPanel.vue"
    view = mod.REPO_ROOT / "frontend/src/views/RunDetailView.vue"
    report = tmp_path / "lcov.info"
    report.write_text(f"SF:{component}\nSF:{view}\n")

    json_present = {
        "src_stats": {
            "frontend/src/components/shared/KnownFixesPanel.vue": {
                "percent_covered": 100.0,
                "covered_lines": list(range(1, 22)),
                "violation_lines": [],
            }
        },
        "total_num_lines": 21,
        "total_num_violations": 0,
        "total_percent_covered": 100.0,
        "num_changed_lines": 24,
    }
    with (
        patch.object(
            mod,
            "_get_changed_production_files",
            return_value={
                "frontend/src/components/shared/KnownFixesPanel.vue": 60,
                "frontend/src/views/RunDetailView.vue": 3,
            },
        ),
        patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)),
        patch.object(mod, "_get_diff_cover_json", return_value=json_present),
    ):
        result = mod.evaluate(
            language="JavaScript",
            report_path=report,
            compare_branch="origin/main",
            fail_under=98,
        )

    assert result.passed is True
    assert result.actual_pct == 100.0
    assert result.unmeasured_lines == 0
    assert result.changed_lines == 21


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
def test_default_threshold_is_98():
    assert mod.COVERAGE_THRESHOLD == 98
    assert mod.BRANCH_COVERAGE_THRESHOLD == 98


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


def test_iter_executable_python_lines_skips_bracket_continuations():
    """Entries added inside a collection literal are not statements.

    Regression: a PR that only added data entries to a module-level list
    (e.g. new seed templates) counted every entry line as executable, so the
    coverage gate charged them as unmeasured 0% lines and failed a diff with
    no coverable code.
    """
    lines = [
        '    {"b": 2},',
        "    {",
        '        "c": 3,',
        "    },",
    ]
    # The collection was opened before this added block (depth 1).
    assert not list(mod._iter_executable_python_lines(lines, initial_depth=1))


def test_iter_executable_python_lines_counts_statement_starts():
    """A multi-line statement counts once, at its start; body lines do not."""
    lines = [
        "resolved_node: dict[str, Any] = {",
        '    "id": x,',
        "}",
        'if resolved_node["node_type"] == "manual":',
        "    resolved_node['a'] = 1",
        "resolved_nodes.append(resolved_node)",
    ]
    assert list(mod._iter_executable_python_lines(lines)) == [
        "resolved_node: dict[str, Any] = {",
        'if resolved_node["node_type"] == "manual":',
        "    resolved_node['a'] = 1",
        "resolved_nodes.append(resolved_node)",
    ]


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
_GIT_TIMEOUT_SECS = 60

# Git hook-context variables injected by pre-commit / git when running
# inside a hook.  When these leak into a scratch-repo subprocess, git
# ignores cwd and targets the real repo — causing the scratch commit to
# fail.  The shared list lives in scripts/git_hook_env.py (FAR-835 review).


def _scratch_git_env() -> dict[str, str]:
    """Return an env dict safe for scratch-repo git operations.

    Inherits the current process environment but strips git hook-context
    variables that would redirect git commands to the enclosing repo.
    """
    return {k: v for k, v in os.environ.items() if k not in GIT_HOOK_CONTEXT_VARS}


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(  # noqa: S603 — trusted fixed git args, test helper
        [_GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=_GIT_TIMEOUT_SECS,
    )


def _init_repo(repo: Path, base_content: str = "def f():\n    return 1\n") -> dict[str, str]:
    """Create a git repo with a ``main`` commit; return an env for commits."""
    env = {
        **_scratch_git_env(),
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


def test_evaluate_data_only_collection_diff_skips_via_real_git_diff(tmp_path):
    """A real diff adding entries inside an existing list yields zero executable lines.

    Exercises the bracket-state tracker end-to-end (real ``git diff``,
    unpatched ``_get_changed_production_files``/``_count_added_lines``): the
    added entries continue a literal opened before the hunk, so a pre-fix
    count would treat them as executable and fail the gate.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _init_repo(repo, 'DATA = [\n    {"a": 1},\n]\n')
    entries = "".join(f'    {{"entry_{i}": {i}}},\n' for i in range(12))
    feature_content = f'DATA = [\n    {{"a": 1}},\n{entries}]\n'
    (repo / "src" / "module.py").write_text(feature_content, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Branch coverage: parser unit tests
# ---------------------------------------------------------------------------
class TestParseCoberturaBranches:
    def test_parses_branch_conditions(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="src/main.py"><lines>'
            '<line number="5" hits="3" branch="true" condition-coverage="50% (1/2)">'
            "<conditions>"
            '<condition number="0" type="jump" coverage="100%"/>'
            '<condition number="1" type="jump" coverage="0%"/>'
            "</conditions></line>"
            '<line number="10" hits="1" branch="false"/>'
            "</lines></class></classes></package></packages></coverage>"
        )
        result = mod._parse_cobertura_branches(xml)
        assert "src/main.py" in result
        assert result["src/main.py"][5] == (1, 2)
        assert 10 not in result["src/main.py"]

    def test_parses_condition_coverage_attribute(self, tmp_path):
        """coverage.py ≥7.x emits condition-coverage attributes without <condition> children.

        This is the REAL format from CI (coverage.py 7.16.1): lines have
        ``branch="true" condition-coverage="100% (2/2)"`` as self-closing
        elements with NO child ``<condition>`` elements.
        """
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="src/main.py"><lines>'
            '<line number="5" hits="3" branch="true" condition-coverage="50% (1/2)"/>'
            '<line number="10" hits="1" branch="true" condition-coverage="100% (2/2)"/>'
            '<line number="15" hits="1" branch="false"/>'
            "</lines></class></classes></package></packages></coverage>"
        )
        result = mod._parse_cobertura_branches(xml)
        assert "src/main.py" in result
        assert result["src/main.py"][5] == (1, 2)
        assert result["src/main.py"][10] == (2, 2)
        assert 15 not in result["src/main.py"]

    def test_empty_report(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text("<coverage/>")
        assert not mod._parse_cobertura_branches(xml)

    def test_invalid_xml(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text("not xml")
        assert not mod._parse_cobertura_branches(xml)


class TestParseConditionCoverageAttr:
    """Unit tests for _parse_condition_coverage_attr (the attribute parser)."""

    def test_full_coverage(self):
        assert mod._parse_condition_coverage_attr("100% (2/2)") == (2, 2)

    def test_partial_coverage(self):
        assert mod._parse_condition_coverage_attr("50% (1/2)") == (1, 2)

    def test_zero_coverage(self):
        assert mod._parse_condition_coverage_attr("0% (0/3)") == (0, 3)

    def test_single_condition(self):
        assert mod._parse_condition_coverage_attr("100% (1/1)") == (1, 1)

    def test_unparseable_returns_none(self):
        assert mod._parse_condition_coverage_attr("") is None
        assert mod._parse_condition_coverage_attr("n/a") is None

    def test_strips_whitespace(self):
        assert mod._parse_condition_coverage_attr("  50% (1/2)  ") == (1, 2)


class TestParseLcovBranches:
    def test_parses_brda_records(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/app.ts\nBRDA:5,0,0,1\nBRDA:5,0,1,-\nBRDA:10,0,0,-\nend_of_record\n")
        result = mod._parse_lcov_branches(lcov)
        assert result["src/app.ts"][5] == (1, 2)
        assert result["src/app.ts"][10] == (0, 1)

    def test_taken_zero_is_not_covered(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/x.ts\nBRDA:1,0,0,0\nend_of_record\n")
        result = mod._parse_lcov_branches(lcov)
        assert result["src/x.ts"][1] == (0, 1)

    def test_non_numeric_taken_is_not_covered(self, tmp_path):
        """A malformed ``taken`` value must be counted as uncovered, not crash.

        ``int()`` on a non-numeric field raised an uncaught ``ValueError`` that
        took down the whole gate instead of the structured fail-closed path.
        """
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/x.ts\nBRDA:1,0,0,oops\nBRDA:2,0,0,5\nend_of_record\n")
        result = mod._parse_lcov_branches(lcov)
        assert result["src/x.ts"][1] == (0, 1)
        assert result["src/x.ts"][2] == (1, 1)

    def test_non_numeric_line_number_is_skipped(self, tmp_path):
        """A malformed line number cannot be attributed; the record is dropped."""
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/x.ts\nBRDA:x,0,0,1\nBRDA:3,0,0,1\nend_of_record\n")
        result = mod._parse_lcov_branches(lcov)
        assert result["src/x.ts"] == {3: (1, 1)}

    def test_empty_report(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nend_of_record\n")
        assert not mod._parse_lcov_branches(lcov)


class TestParseAddedLineNumbers:
    def test_extracts_added_line_numbers(self):
        diff = "@@ -1,3 +1,5 @@\n context\n+added_one\n+added_two\n context\n"
        assert mod._parse_added_line_numbers(diff) == {2, 3}

    def test_multiple_hunks(self):
        diff = "@@ -1,2 +1,3 @@\n+a1\n ctx\n@@ -10,2 +11,3 @@\n+b1\n ctx\n"
        assert mod._parse_added_line_numbers(diff) == {1, 11}


# ---------------------------------------------------------------------------
# Branch coverage: evaluation tests -- the cases that catch the rejected impl
# ---------------------------------------------------------------------------
class TestBranchCoverageEvaluation:
    def test_two_branches_one_taken_fails(self, tmp_path):
        """CATCHES THE REJECTED IMPLEMENTATION: 2 branches, 1 taken -> 50% -> FAIL."""
        fake_report = tmp_path / "coverage.xml"
        fake_report.write_text("<coverage/>")
        json_pass = {
            "src_stats": {
                "src/calc.py": {"percent_covered": 100.0, "covered_lines": list(range(50)), "violation_lines": []}
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
            patch.object(mod, "_compute_branch_coverage_from_raw", return_value=(1, 2, 0)),
        ):
            result = mod.evaluate("Python", fake_report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is True
        assert result.branch_passed is False
        assert result.branch_actual_pct == 50.0

    def test_all_branches_not_taken_fails(self, tmp_path):
        """Branch records present but ALL taken='-' -> 0% -> FAIL."""
        fake_report = tmp_path / "coverage.xml"
        fake_report.write_text("<coverage/>")
        json_pass = {
            "src_stats": {
                "src/calc.py": {"percent_covered": 100.0, "covered_lines": list(range(50)), "violation_lines": []}
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
            patch.object(mod, "_compute_branch_coverage_from_raw", return_value=(0, 4, 0)),
        ):
            result = mod.evaluate("Python", fake_report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is True
        assert result.branch_passed is False
        assert result.branch_actual_pct == 0.0

    def test_line_97_percent_fails_new_threshold(self, tmp_path):
        """Line 97% -> FAIL (new 98 threshold)."""
        fake_report = tmp_path / "coverage.xml"
        fake_report.write_text("<coverage/>")
        json_97 = {
            "src_stats": {
                "src/calc.py": {
                    "percent_covered": 100.0,
                    "covered_lines": list(range(97)),
                    "violation_lines": [98, 99, 100],
                }
            },
            "total_num_lines": 100,
            "total_num_violations": 3,
            "total_percent_covered": 97.0,
            "num_changed_lines": 100,
        }
        with (
            patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 100}),
            patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_90_STDOUT)),
            patch.object(mod, "_get_diff_cover_json", return_value=json_97),
            patch.object(mod, "_compute_branch_coverage_from_raw", return_value=(100, 100, 0)),
        ):
            result = mod.evaluate("Python", fake_report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is False
        assert result.actual_pct is not None
        assert result.actual_pct < 98

    def test_line_99_branch_90_fails(self, tmp_path):
        """Line 99% + branch 90% -> FAIL."""
        fake_report = tmp_path / "coverage.xml"
        fake_report.write_text("<coverage/>")
        json_99 = {
            "src_stats": {
                "src/calc.py": {"percent_covered": 100.0, "covered_lines": list(range(99)), "violation_lines": [100]}
            },
            "total_num_lines": 100,
            "total_num_violations": 1,
            "total_percent_covered": 99.0,
            "num_changed_lines": 100,
        }
        with (
            patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 100}),
            patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)),
            patch.object(mod, "_get_diff_cover_json", return_value=json_99),
            patch.object(mod, "_compute_branch_coverage_from_raw", return_value=(9, 10, 0)),
        ):
            result = mod.evaluate("Python", fake_report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is True
        assert result.branch_passed is False

    def test_line_99_branch_99_passes(self, tmp_path):
        """Line 99% + branch 99% -> PASS."""
        fake_report = tmp_path / "coverage.xml"
        fake_report.write_text("<coverage/>")
        json_99 = {
            "src_stats": {
                "src/calc.py": {"percent_covered": 100.0, "covered_lines": list(range(99)), "violation_lines": [100]}
            },
            "total_num_lines": 100,
            "total_num_violations": 1,
            "total_percent_covered": 99.0,
            "num_changed_lines": 100,
        }
        with (
            patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 100}),
            patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)),
            patch.object(mod, "_get_diff_cover_json", return_value=json_99),
            patch.object(mod, "_compute_branch_coverage_from_raw", return_value=(99, 100, 0)),
        ):
            result = mod.evaluate("Python", fake_report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is True
        assert result.branch_passed is True

    def test_vacuous_branch_pass(self, tmp_path):
        """No branch records -> vacuous pass."""
        fake_report = tmp_path / "coverage.xml"
        fake_report.write_text("<coverage/>")
        json_pass = {
            "src_stats": {
                "src/calc.py": {"percent_covered": 100.0, "covered_lines": list(range(50)), "violation_lines": []}
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
            patch.object(mod, "_compute_branch_coverage_from_raw", return_value=(0, 0, 0)),
        ):
            result = mod.evaluate("Python", fake_report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is True
        assert result.branch_passed is None
        assert result.branch_actual_pct is None

    def test_zero_branch_records_with_unmeasured_lines_is_vacuous_pass(self, tmp_path):
        """``(branch_total=0, branch_unmeasured>0)`` -> vacuous pass, not 0% FAIL.

        Regression for the docstring/code contradiction: a fully line-covered
        straight-line (branch-free) diff produces no branch records on any
        changed line, so ``_compute_branch_coverage_from_raw`` returns
        ``branch_total=0`` with ``branch_unmeasured>0``.  The gate used to score
        that as 0% and fail any branch-free diff longer than the tiny-diff
        threshold; the documented intent is a vacuous pass.
        """
        fake_report = tmp_path / "coverage.xml"
        fake_report.write_text("<coverage/>")
        json_pass = {
            "src_stats": {
                "src/calc.py": {"percent_covered": 100.0, "covered_lines": list(range(50)), "violation_lines": []}
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
            patch.object(mod, "_compute_branch_coverage_from_raw", return_value=(0, 0, 50)),
        ):
            result = mod.evaluate("Python", fake_report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is True
        assert result.branch_passed is None
        assert result.branch_actual_pct is None
        assert result.branch_unmeasured == 50
        assert "vacuous" in result.summary().lower()

    def test_straight_line_diff_passes_branch_gate_vacuously(self, tmp_path):
        """A real straight-line diff with no branch records must not 0% FAIL.

        Drives the unpatched ``_compute_branch_coverage_from_raw`` path: the
        Cobertura report lists the changed file but carries no branch records
        (every line ``branch="false"``), so ``branch_total`` is 0 while
        ``branch_unmeasured`` is the changed-line count.  The branch gate must
        pass vacuously rather than fail the branch-free diff at 0%.
        """
        report = tmp_path / "coverage.xml"
        report.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="src/calc.py"><lines>'
            '<line number="1" hits="1"/><line number="2" hits="1"/>'
            '<line number="3" hits="1"/></lines></class></classes></package></packages></coverage>'
        )
        json_pass = {
            "src_stats": {
                "src/calc.py": {"percent_covered": 100.0, "covered_lines": list(range(1, 51)), "violation_lines": []}
            },
            "total_num_lines": 50,
            "num_changed_lines": 50,
            "total_percent_covered": 100.0,
        }
        git_diff = "@@ -0,0 +1,50 @@\n" + "\n".join(f"+line{i}" for i in range(1, 51)) + "\n"
        with (
            patch.object(mod, "_get_changed_production_files", return_value={"src/calc.py": 50}),
            patch.object(mod, "_run_diff_cover", return_value=(0, REAL_DIFF_COVER_PASS_STDOUT)),
            patch.object(mod, "_get_diff_cover_json", return_value=json_pass),
            patch.object(mod.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            result = mod.evaluate("Python", report, "origin/main", 98, branch_fail_under=98)
        assert result.passed is True
        assert result.branch_total == 0
        assert result.branch_unmeasured > 0
        assert result.branch_passed is None
        assert result.branch_actual_pct is None


# ---------------------------------------------------------------------------
# Branch parser integration tests
# ---------------------------------------------------------------------------
class TestBranchParserIntegration:
    def test_cobertura_all_conditions_covered(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="src/main.py"><lines>'
            '<line number="5" hits="2" branch="true" condition-coverage="100% (2/2)">'
            '<conditions><condition number="0" type="jump" coverage="100%"/>'
            '<condition number="1" type="jump" coverage="100%"/></conditions></line>'
            "</lines></class></classes></package></packages></coverage>"
        )
        assert mod._parse_cobertura_branches(xml)["src/main.py"][5] == (2, 2)

    def test_cobertura_no_conditions_covered(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="src/main.py"><lines>'
            '<line number="5" hits="0" branch="true" condition-coverage="0% (0/2)">'
            '<conditions><condition number="0" type="jump" coverage="0%"/>'
            '<condition number="1" type="jump" coverage="0%"/></conditions></line>'
            "</lines></class></classes></package></packages></coverage>"
        )
        assert mod._parse_cobertura_branches(xml)["src/main.py"][5] == (0, 2)

    def test_lcov_mixed_taken_not_taken(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/app.ts\nBRDA:3,0,0,1\nBRDA:3,0,1,2\nBRDA:7,0,0,-\nend_of_record\n")
        result = mod._parse_lcov_branches(lcov)
        assert result["src/app.ts"][3] == (2, 2)
        assert result["src/app.ts"][7] == (0, 1)

    def test_compute_branch_coverage_from_raw_cobertura(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="src/main.py"><lines>'
            '<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)">'
            '<conditions><condition number="0" type="jump" coverage="100%"/>'
            '<condition number="1" type="jump" coverage="0%"/></conditions></line>'
            '<line number="2" hits="1" branch="true" condition-coverage="100% (1/1)">'
            '<conditions><condition number="0" type="jump" coverage="100%"/></conditions></line>'
            "</lines></class></classes></package></packages></coverage>"
        )
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"src/main.py": 2}, xml, "origin/main", "Python"
            )
        assert covered == 2
        assert total == 3
        assert unmeasured == 0

    def test_compute_branch_coverage_from_raw_lcov(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/app.ts\nBRDA:1,0,0,1\nBRDA:1,0,1,-\nBRDA:2,0,0,3\nend_of_record\n")
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"src/app.ts": 2}, lcov, "origin/main", "JavaScript"
            )
        assert covered == 2
        assert total == 3
        assert unmeasured == 0

    def test_compute_branch_coverage_unmeasured_file(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/other.ts\nBRDA:1,0,0,1\nend_of_record\n")
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"src/new.ts": 2}, lcov, "origin/main", "JavaScript"
            )
        assert covered == 0
        assert total == 0
        assert unmeasured == 2

    # ------------------------------------------------------------------
    # Realistic report-path alignment (the branch gate could only ever pass
    # vacuously or fail with a false 0% before this fix).
    # ------------------------------------------------------------------
    def test_compute_branch_coverage_matches_python_source_root_relative_key(self, tmp_path):
        """coverage.py emits Cobertura filenames relative to its source root.

        This repo runs ``--cov=src/modulo`` from ``backend/``, so a changed
        ``backend/src/modulo/core/foo.py`` appears in the report as
        ``core/foo.py``.  Before the path-alignment fix the lookup was a plain
        dict get on the repo-relative key, so every branch record was
        discarded and the file scored 0%.
        """
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="core/foo.py"><lines>'
            '<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)">'
            '<conditions><condition number="0" type="jump" coverage="100%"/>'
            '<condition number="1" type="jump" coverage="0%"/></conditions></line>'
            '<line number="2" hits="1" branch="true" condition-coverage="100% (1/1)">'
            '<conditions><condition number="0" type="jump" coverage="100%"/></conditions></line>'
            "</lines></class></classes></package></packages></coverage>"
        )
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"backend/src/modulo/core/foo.py": 2}, xml, "origin/main", "Python"
            )
        assert covered == 2
        assert total == 3
        assert unmeasured == 0

    def test_compute_branch_coverage_matches_python_backend_relative_key(self, tmp_path):
        """A report path relative to ``backend/`` also aligns to the repo key."""
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="src/modulo/foo.py"><lines>'
            '<line number="1" hits="1" branch="true" condition-coverage="100% (1/1)">'
            '<conditions><condition number="0" type="jump" coverage="100%"/></conditions></line>'
            "</lines></class></classes></package></packages></coverage>"
        )
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"backend/src/modulo/foo.py": 2}, xml, "origin/main", "Python"
            )
        assert covered == 1
        assert total == 1
        assert unmeasured == 1

    def test_compute_branch_coverage_matches_absolute_js_report_key(self, tmp_path):
        """The normalised LCOV report carries absolute ``SF:`` paths.

        ``main`` rewrites the frontend report via ``_normalize_js_report``
        before evaluation, so branch data arrives keyed by absolute path while
        the diff keys are ``frontend/src/...``.  Before the path-alignment fix
        a changed JS production file failed the branch gate at a false 0%.
        """
        abs_key = (mod.REPO_ROOT / "frontend" / "src" / "app.ts").as_posix()
        lcov = tmp_path / "lcov.info"
        lcov.write_text(f"TN:\nSF:{abs_key}\nBRDA:1,0,0,1\nBRDA:1,0,1,-\nBRDA:2,0,0,3\nend_of_record\n")
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"frontend/src/app.ts": 2}, lcov, "origin/main", "JavaScript"
            )
        assert covered == 2
        assert total == 3
        assert unmeasured == 0

    def test_compute_branch_coverage_absolute_js_all_not_taken(self, tmp_path):
        """Realistic path + every branch ``taken='-'`` -> 0% but NOT unmeasured.

        This is the all-taken-``'-'`` arm of the branch gate: the branches
        exist on changed lines and were never executed, so the total is real
        and the coverage is a genuine 0% (not a vacuous pass).
        """
        abs_key = (mod.REPO_ROOT / "frontend" / "src" / "app.ts").as_posix()
        lcov = tmp_path / "lcov.info"
        lcov.write_text(f"TN:\nSF:{abs_key}\nBRDA:1,0,0,-\nBRDA:1,0,1,-\nend_of_record\n")
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"frontend/src/app.ts": 2}, lcov, "origin/main", "JavaScript"
            )
        assert covered == 0
        assert total == 2
        assert unmeasured == 1

    def test_compute_branch_coverage_no_match_with_realistic_paths(self, tmp_path):
        """A report file that is not a changed production file stays unmeasured."""
        abs_key = (mod.REPO_ROOT / "frontend" / "src" / "other.ts").as_posix()
        lcov = tmp_path / "lcov.info"
        lcov.write_text(f"TN:\nSF:{abs_key}\nBRDA:1,0,0,1\nend_of_record\n")
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"frontend/src/new.ts": 2}, lcov, "origin/main", "JavaScript"
            )
        assert covered == 0
        assert total == 0
        assert unmeasured == 2

    def test_compute_branch_coverage_honours_custom_js_src_root(self, tmp_path):
        """An explicit JS source root must drive key alignment, not the default.

        ``--js-src-root`` exists so a report whose relative ``SF:`` paths
        resolve against a directory other than ``frontend/`` still aligns to
        the repo-relative changed-file keys.  The old implementation threaded
        only the default root into the alignment, silently ignoring the flag,
        so a custom root scored the file as unmeasured.
        """
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:src/app.ts\nBRDA:1,0,0,1\nBRDA:1,0,1,-\nend_of_record\n")
        git_diff = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        with patch.object(mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_diff, stderr="")
            covered, total, unmeasured = mod._compute_branch_coverage_from_raw(
                {"webapp/src/app.ts": 2}, lcov, "origin/main", "JavaScript", js_src_root="webapp"
            )
        assert covered == 1
        assert total == 2
        assert unmeasured == 1

    def test_match_report_key_rejects_ambiguous_suffix(self):
        """A bare basename matching multiple changed files must not cross-attribute."""
        changed = ["backend/src/modulo/a/foo.py", "backend/src/modulo/b/foo.py"]
        assert mod._match_report_key_to_changed("foo.py", changed, "Python") is None
        assert mod._match_report_key_to_changed("a/foo.py", changed, "Python") == "backend/src/modulo/a/foo.py"


# ---------------------------------------------------------------------------
# Project-wide floor tests
# ---------------------------------------------------------------------------
class TestProjectWideMetrics:
    def test_cobertura_metrics(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>'
            '<line number="1" hits="3"/><line number="2" hits="0"/>'
            '<line number="3" hits="1" branch="true" condition-coverage="100% (1/1)">'
            '<conditions><condition number="0" type="jump" coverage="100%"/></conditions></line>'
            '<line number="4" hits="0" branch="true" condition-coverage="0% (0/1)">'
            '<conditions><condition number="0" type="jump" coverage="0%"/></conditions></line>'
            "</lines></class>"
            '<class filename="b.py"><lines><line number="1" hits="5"/></lines></class>'
            "</classes></package></packages></coverage>"
        )
        line_pct, branch_pct = mod._compute_project_wide_metrics_cobertura(xml)
        assert line_pct == 60.0
        assert branch_pct == 50.0

    def test_cobertura_metrics_root_attributes(self, tmp_path):
        """coverage.py ≥7.x puts branch totals on the root <coverage> element.

        The parser must read ``branches-covered`` / ``branches-valid`` from
        the root element when ``<condition>`` children are absent.
        """
        xml = tmp_path / "coverage.xml"
        # Simulate real coverage.py output: 4 lines (3 hit), root attrs for branch.
        xml.write_text(
            '<coverage branches-valid="26766" branches-covered="23999" branch-rate="0.8966">'
            "<packages><package><classes>"
            '<class filename="a.py"><lines>'
            '<line number="1" hits="3"/><line number="2" hits="0"/>'
            '<line number="3" hits="1" branch="true" condition-coverage="100% (2/2)"/>'
            '<line number="4" hits="0" branch="true" condition-coverage="50% (1/2)"/>'
            "</lines></class>"
            '<class filename="b.py"><lines><line number="1" hits="5"/></lines></class>'
            "</classes></package></packages></coverage>"
        )
        line_pct, branch_pct = mod._compute_project_wide_metrics_cobertura(xml)
        assert line_pct == 60.0
        # Root attrs: 23999/26766 = 89.66%
        assert branch_pct is not None
        assert abs(branch_pct - 89.66) < 0.01

    def test_cobertura_metrics_condition_attr_fallback(self, tmp_path):
        """When root attrs are absent, branch % is computed from condition-coverage attrs."""
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>'
            '<line number="1" hits="3"/><line number="2" hits="0"/>'
            '<line number="3" hits="1" branch="true" condition-coverage="100% (2/2)"/>'
            '<line number="4" hits="0" branch="true" condition-coverage="0% (0/2)"/>'
            "</lines></class>"
            '<class filename="b.py"><lines><line number="1" hits="5"/></lines></class>'
            "</classes></package></packages></coverage>"
        )
        line_pct, branch_pct = mod._compute_project_wide_metrics_cobertura(xml)
        assert line_pct == 60.0
        # 4 total conditions, 2 covered = 50%
        assert branch_pct == 50.0

    def test_lcov_metrics(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text(
            "TN:\nSF:a.ts\nDA:1,1\nDA:2,0\nDA:3,5\nBRDA:1,0,0,1\nBRDA:2,0,0,-\nend_of_record\n"
            "TN:\nSF:b.ts\nDA:1,1\nend_of_record\n"
        )
        line_pct, branch_pct = mod._compute_project_wide_metrics_lcov(lcov)
        assert line_pct == 75.0
        assert branch_pct == 50.0

    def test_cobertura_empty(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        xml.write_text("<coverage/>")
        # No branch records at all -> branch is None (absent), not 0.0.
        assert mod._compute_project_wide_metrics_cobertura(xml) == (0.0, None)

    def test_lcov_empty(self, tmp_path):
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nend_of_record\n")
        # No branch records at all -> branch is None (absent), not 0.0.
        assert mod._compute_project_wide_metrics_lcov(lcov) == (0.0, None)

    def test_lcov_non_numeric_counts_do_not_crash(self, tmp_path):
        """Malformed DA/BRDA numeric fields must not raise — they count as unhit.

        The project-wide floor read ``float(taken_str)`` / ``int(parts[1])``
        unguarded, so a single non-numeric field crashed the gate rather than
        letting it fail closed with structured output.
        """
        lcov = tmp_path / "lcov.info"
        lcov.write_text("TN:\nSF:a.ts\nDA:1,oops\nDA:2,3\nBRDA:1,0,0,bad\nBRDA:2,0,0,2\nend_of_record\n")
        assert mod._compute_project_wide_metrics_lcov(lcov) == (50.0, 50.0)

    def test_cobertura_zero_branch_with_data_is_zero_not_absent(self, tmp_path):
        """Branch records that exist but are all uncovered report 0.0, not None."""
        xml = tmp_path / "coverage.xml"
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>'
            '<line number="1" hits="0" branch="true" condition-coverage="0% (0/1)">'
            '<conditions><condition number="0" type="jump" coverage="0%"/></conditions></line>'
            "</lines></class></classes></package></packages></coverage>"
        )
        assert mod._compute_project_wide_metrics_cobertura(xml) == (0.0, 0.0)


class TestProjectWideFloor:
    def test_project_below_floor_skipped_when_no_production_changes(self, tmp_path, capsys):
        """No production changes -> all languages skip -> but floor IS enforced.

        The project floor is a guard on main's coverage regardless of what the
        PR changed.  Even when all languages skip (no production changes), a
        report with line coverage below the floor must still fail the gate.
        """
        xml = tmp_path / "coverage.xml"
        lines = "\n".join('<line number="{}" hits="{}"/>'.format(i, "1" if i <= 80 else "0") for i in range(1, 101))
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )
        with (
            patch.object(mod, "_get_changed_production_files", return_value={}),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 1
        out = capsys.readouterr().out
        assert "BREACH" in out

    def test_project_line_below_floor(self, tmp_path):
        """Line coverage 80% < floor 88% → FAIL (exit 1)."""
        xml = tmp_path / "coverage.xml"
        lines = "\n".join('<line number="{}" hits="{}"/>'.format(i, "1" if i <= 80 else "0") for i in range(1, 101))
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )
        with (
            patch.object(mod, "_get_changed_production_files", return_value={}),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 1

    def test_project_line_below_floor_fails(self, tmp_path, capsys):
        """Production changes + project line coverage below floor -> exit 1.

        The floor-failure path (exit 1) was untested: the old
        ``test_project_line_below_floor`` drove the all-skipped short-circuit.
        Here a Python production file changes (tiny diff, so the changed-lines
        gate passes) while the whole-project line rate (80%) sits below the
        88% floor -> the run must fail.
        """
        xml = tmp_path / "coverage.xml"
        lines = "\n".join('<line number="{}" hits="{}"/>'.format(i, "1" if i <= 80 else "0") for i in range(1, 101))
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )

        def fake_changed(compare_branch, language):
            return {"backend/src/modulo/x.py": 1} if language == "Python" else {}

        with (
            patch.object(mod, "_get_changed_production_files", side_effect=fake_changed),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()

        assert rc == 1
        out = capsys.readouterr().out
        assert "BREACH: Python line" in out
        assert "WARNING" not in out

    def test_project_js_floor_enforced_with_relative_lcov_paths(self, tmp_path, capsys):
        """The JS floor must be read from the raw report, not the deleted temp copy.

        ``main`` normalises the LCOV report (vitest emits relative ``SF:``
        paths) into an absolute-path temp file and unlinks it in the ``finally``
        before the project-wide floor check runs.  Reading the normalised path
        therefore finds a deleted file and silently skips the JavaScript floor
        on every CI run; the floor must read the original report instead.
        """
        lcov = tmp_path / "lcov.info"
        # Relative SF path => _normalize_js_report writes a temp copy that
        # main deletes before the floor check.  50% line coverage sits below
        # the 88% floor, so the run must fail.
        lcov.write_text("TN:\nSF:src/App.vue\nDA:1,1\nDA:2,0\nend_of_record\n")

        def fake_changed(compare_branch, language):
            # A Python tiny diff keeps the gate from short-circuiting on
            # all-skipped; JavaScript has no changed production files.
            return {"backend/src/modulo/x.py": 1} if language == "Python" else {}

        with (
            patch.object(mod, "_get_changed_production_files", side_effect=fake_changed),
            patch(
                "sys.argv",
                [
                    "run_coverage_gate.py",
                    "--compare-branch",
                    "origin/main",
                    "--python-report",
                    str(tmp_path / "no-python.xml"),
                    "--js-report",
                    str(lcov),
                ],
            ),
        ):
            rc = mod.main()

        assert rc == 1
        out = capsys.readouterr().out
        assert "BREACH: JavaScript line" in out

    def test_project_branch_zero_with_data_fails(self, tmp_path):
        """Branch records present but all uncovered (0%) -> floor failure.

        ``pb > 0`` excluded a genuine 0% branch rate from the floor, exactly
        the "all branches uncovered" case the per-PR branch gate treats as a
        real failure (not vacuous).  With line coverage above the floor and
        branch at 0% with data, the run must fail.
        """
        xml = tmp_path / "coverage.xml"
        lines = "\n".join(
            f'<line number="{i}" hits="1" branch="true" '
            'condition-coverage="0% (0/1)">'
            '<conditions><condition number="0" type="jump" '
            'coverage="0%"/></conditions></line>'
            for i in range(1, 101)
        )
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )

        def fake_changed(compare_branch, language):
            return {"backend/src/modulo/x.py": 1} if language == "Python" else {}

        with (
            patch.object(mod, "_get_changed_production_files", side_effect=fake_changed),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 1

    def test_project_no_branch_data_fails_closed(self, tmp_path, capsys):
        """No branch records at all -> branch floor FAILS CLOSED (exit 1).

        FAR-1063: a report with no branch data is indistinguishable from one
        whose branch data was dropped.  The gate must fail with a distinct
        reason, not silently pass.
        """
        xml = tmp_path / "coverage.xml"
        lines = "\n".join(f'<line number="{i}" hits="1"/>' for i in range(1, 101))
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )

        def fake_changed(compare_branch, language):
            return {"backend/src/modulo/x.py": 1} if language == "Python" else {}

        with (
            patch.object(mod, "_get_changed_production_files", side_effect=fake_changed),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 1, "Missing branch data must fail closed, not pass"
        out = capsys.readouterr().out
        assert "BREACH" in out
        assert "branch data missing" in out.lower()

    def test_project_both_above_floor(self, tmp_path):
        xml = tmp_path / "coverage.xml"
        lines = "\n".join(
            f'<line number="{i}" hits="1" branch="true" '
            'condition-coverage="100% (1/1)">'
            '<conditions><condition number="0" type="jump" '
            'coverage="100%"/></conditions></line>'
            for i in range(1, 101)
        )
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )
        with (
            patch.object(mod, "_get_changed_production_files", return_value={}),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 0


# ---------------------------------------------------------------------------
# Branch coverage in summary output
# ---------------------------------------------------------------------------
class TestBranchCoverageSummary:
    def test_summary_includes_branch_info(self):
        result = mod.GateResult(
            language="Python",
            skipped=False,
            skip_reason="",
            passed=True,
            actual_pct=100.0,
            threshold=98,
            branch_actual_pct=95.0,
            branch_passed=False,
            branch_threshold=98,
        )
        summary = result.summary()
        assert "[Python] PASS" in summary
        assert "branch 95.0%" in summary

    def test_summary_vacuous_branch(self):
        result = mod.GateResult(
            language="JavaScript",
            skipped=False,
            skip_reason="",
            passed=True,
            actual_pct=100.0,
            threshold=98,
            branch_passed=None,
        )
        assert "vacuous" in result.summary().lower()

    def test_summary_skipped_no_branch_info(self):
        result = mod.GateResult(
            language="Python",
            skipped=True,
            skip_reason="no changed production lines",
            passed=True,
            actual_pct=None,
            threshold=98,
        )
        summary = result.summary()
        assert "SKIPPED" in summary
        assert "branch" not in summary.lower()

    def test_summary_tiny_diff_is_line_only(self):
        """Tiny diffs never measure branches, so the summary must not claim one.

        ``evaluate`` returns before ``_compute_branch_coverage_from_raw`` runs,
        so ``branch_passed`` is always None on a tiny diff.  Reporting a
        branch-aware line while branches are unmeasured was contradictory —
        and the table's "FAIL (branch)" tiny-diff status was unreachable.
        """
        result = mod.GateResult(
            language="Python",
            skipped=False,
            skip_reason="",
            passed=True,
            actual_pct=None,
            threshold=98,
            changed_lines=2,
            tiny_diff=True,
            branch_passed=None,
        )
        summary = result.summary()
        assert "tiny diff" in summary
        assert "branch" not in summary.lower()

    def test_tiny_diff_result_has_no_branch_measurement(self, tmp_path):
        """A tiny diff is line-only: branch_passed stays None by construction."""
        with patch.object(mod, "_get_changed_production_files", return_value={"src/main.py": 1}):
            result = mod.evaluate("Python", tmp_path / "missing.xml", "origin/main", 98, branch_fail_under=98)
        assert result.tiny_diff is True
        assert result.branch_passed is None
        assert result.branch_actual_pct is None


# ---------------------------------------------------------------------------
# FAR-1063: fail-closed branch floor + no "WARNING" in output
# ---------------------------------------------------------------------------
class TestFar1063BranchFloorFailClosed:
    """FAR-1063: branch data missing must fail closed (distinct from floor breach),
    and floor breaches must not print 'WARNING'."""

    def test_python_branch_below_floor_fails(self, tmp_path):
        """Branch data present but below floor → FAIL."""
        xml = tmp_path / "coverage.xml"
        # 100 lines, all hit (100% line), but only 1 of 4 branches covered (25% branch)
        lines = "\n".join(
            f'<line number="{i}" hits="1" branch="true" '
            'condition-coverage="25% (1/4)">'
            "<conditions>"
            '<condition number="0" type="jump" coverage="100%"/>'
            '<condition number="1" type="jump" coverage="0%"/>'
            '<condition number="2" type="jump" coverage="0%"/>'
            '<condition number="3" type="jump" coverage="0%"/>'
            "</conditions></line>"
            for i in range(1, 101)
        )
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )
        with (
            patch.object(mod, "_get_changed_production_files", return_value={}),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 1

    def test_python_no_branch_data_fails_closed(self, tmp_path, capsys):
        """Cobertura report with NO branch data → FAIL (exit 1) with distinct
        reason — NOT a floor breach, NOT a pass.  This is the FAR-1063 defect:
        the old code silently passed when pb == 0.0."""
        xml = tmp_path / "coverage.xml"
        # 100 lines, all hit, no branch="true" attributes at all
        lines = "\n".join(f'<line number="{i}" hits="1"/>' for i in range(1, 101))
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )
        with (
            patch.object(mod, "_get_changed_production_files", return_value={}),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 1, "Missing branch data must fail closed, not pass"
        captured = capsys.readouterr()
        assert "branch data missing" in captured.out.lower(), "Missing branch data must produce its own distinct reason"
        # Must NOT be reported as a coverage breach of the branch metric
        assert "branch 0.0% < floor" not in captured.out.lower()

    def test_js_no_branch_data_fails_closed(self, tmp_path, capsys):
        """LCOV report with NO BRDA records → FAIL (exit 1) with distinct reason."""
        lcov = tmp_path / "lcov.info"
        # DA records (line data) but no BRDA records (no branch data)
        lcov.write_text("TN:\nSF:a.ts\n" + "\n".join(f"DA:{i},1" for i in range(1, 101)) + "\nend_of_record\n")
        with (
            patch.object(mod, "_get_changed_production_files", return_value={}),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--js-report", str(lcov)],
            ),
        ):
            rc = mod.main()
        assert rc == 1, "Missing branch data must fail closed, not pass"
        captured = capsys.readouterr()
        assert "branch data missing" in captured.out.lower(), "Missing branch data must produce its own distinct reason"

    def test_floor_breach_output_no_warning(self, tmp_path, capsys):
        """A floor breach's output must NOT contain the word 'WARNING'."""
        xml = tmp_path / "coverage.xml"
        lines = "\n".join(
            f'<line number="{i}" hits="1" branch="true" '
            'condition-coverage="25% (1/4)">'
            "<conditions>"
            '<condition number="0" type="jump" coverage="100%"/>'
            '<condition number="1" type="jump" coverage="0%"/>'
            '<condition number="2" type="jump" coverage="0%"/>'
            '<condition number="3" type="jump" coverage="0%"/>'
            "</conditions></line>"
            for i in range(1, 101)
        )
        xml.write_text(
            "<coverage><packages><package><classes>"
            '<class filename="a.py"><lines>' + lines + "</lines></class></classes></package></packages></coverage>"
        )
        with (
            patch.object(mod, "_get_changed_production_files", return_value={}),
            patch(
                "sys.argv",
                ["run_coverage_gate.py", "--compare-branch", "origin/main", "--python-report", str(xml)],
            ),
        ):
            rc = mod.main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "WARNING" not in captured.out, "Floor breaches must not print 'WARNING' — they are hard failures"
        assert "BREACH" in captured.out, "Floor breaches should print 'BREACH'"
