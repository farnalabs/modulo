"""Unit tests for check_new_code_coverage.py — the FAR-600 new-code coverage gate.

Pure-function tests for cobertura/lcov parsing, path normalisation, exclusion
filtering and unmeasured-file handling, plus main() verdict tests with the
git helpers patched (no real git, no DB, no network).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT: Path | None = None
for _parent in Path(__file__).resolve().parents:
    if (_parent / ".github" / "scripts" / "check_new_code_coverage.py").exists():
        _REPO_ROOT = _parent
        break
if _REPO_ROOT is None:
    raise RuntimeError("Could not find repo root (.github/scripts/check_new_code_coverage.py)")

_SCRIPT_PATH = _REPO_ROOT / ".github" / "scripts" / "check_new_code_coverage.py"
_spec = importlib.util.spec_from_file_location("check_new_code_coverage", _SCRIPT_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Could not load check_new_code_coverage.py spec")
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_new_code_coverage"] = gate
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# Cobertura XML / lcov fixture builders
# ---------------------------------------------------------------------------


def _line_xml(hits: int, number: int, condition: str | None = None) -> str:
    condition_attr = f' condition-coverage="{condition}" branch="true"' if condition else ""
    return f'<line hits="{hits}" number="{number}"{condition_attr}/>'


def _class_xml(filename: str, lines: list[str]) -> str:
    body = "".join(lines)
    return (
        f'<class branch-rate="0" complexity="0" filename="{filename}" line-rate="0.9" name="m.py">'
        f"<methods/><lines>{body}</lines></class>"
    )


def _write_cobertura(path: Path, classes: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" ?>'
        '<coverage branch-rate="0" line-rate="0.9" lines-covered="4" lines-valid="5" version="7.10.6">'
        f"<sources><source>src/modulo</source></sources><packages><package>{''.join(classes)}"
        "</package></packages></coverage>",
        encoding="utf-8",
    )


def _write_lcov(path: Path, records: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


def _lcov_record(sf: str, lf: int, lh: int) -> str:
    return f"TN:\nSF:{sf}\nLF:{lf}\nLH:{lh}\nend_of_record"


# ---------------------------------------------------------------------------
# pattern_matches / exclusion filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("backend/src/**/*.py", "backend/src/modulo/api/routes.py", True),
        ("backend/src/**/*.py", "backend/src/routes.py", True),
        ("backend/src/**/*.py", "backend/src/modulo/api/routes.ts", False),
        ("backend/src/**/*.py", "backend/tests/unit/test_x.py", False),
        ("frontend/src/**", "frontend/src/stores/auth.ts", True),
        ("frontend/src/**", "frontend/src/lib/api/schema.ts", True),
        ("frontend/src/**", "frontend/tests/e2e/smoke.spec.ts", False),
        ("frontend/src/__tests__/**", "frontend/src/__tests__/auth.spec.ts", True),
        ("frontend/src/__tests__/**", "frontend/src/__tests__/sub/auth.spec.ts", True),
        ("frontend/src/__tests__/**", "frontend/src/auth.spec.ts", False),
        ("backend/src/modulo/db/migrations/**", "backend/src/modulo/db/migrations/0001_init.py", True),
        ("backend/scripts/**", "backend/scripts/seed.py", True),
        ("backend/tools/**", "backend/tools/probe.py", True),
        ("backend/src/modulo/db/rls.py", "backend/src/modulo/db/rls.py", True),
        ("backend/src/modulo/db/rls.py", "backend/src/modulo/db/rls2.py", False),
    ],
    ids=[
        "backend-py-nested",
        "backend-py-direct",
        "backend-non-py",
        "backend-tests-not-production",
        "frontend-ts",
        "frontend-nested-lib",
        "frontend-tests-not-production",
        "frontend-dunder-tests",
        "frontend-dunder-tests-nested",
        "frontend-outside-dunder-tests",
        "migrations-excluded",
        "backend-scripts-excluded",
        "backend-tools-excluded",
        "exact-file-match",
        "exact-file-no-match",
    ],
)
def test_pattern_matches(pattern, path, expected):
    assert gate.pattern_matches(pattern, path) is expected


def test_filter_changed_production_files_keeps_production_sources():
    changed = [
        "backend/src/modulo/api/routes.py",
        "frontend/src/components/ToggleSwitch.vue",
        "backend/tests/unit/test_routes.py",
        "README.md",
    ]
    assert gate.filter_changed_production_files(changed) == [
        "backend/src/modulo/api/routes.py",
        "frontend/src/components/ToggleSwitch.vue",
    ]


def test_filter_changed_production_files_applies_coverage_exclusions():
    changed = [
        "backend/src/modulo/db/migrations/0130_new_migration.py",
        "backend/scripts/seed.py",
        "backend/tools/probe.py",
        "frontend/src/__tests__/auth.spec.ts",
        "frontend/tests/e2e/smoke.spec.ts",
        "backend/src/modulo/api/routes.py",
        "frontend/src/stores/auth.ts",
    ]
    assert gate.filter_changed_production_files(changed) == [
        "backend/src/modulo/api/routes.py",
        "frontend/src/stores/auth.ts",
    ]


# ---------------------------------------------------------------------------
# report path normalisation
# ---------------------------------------------------------------------------


def test_normalize_report_path_prefixes_backend_src():
    assert gate.normalize_report_path("src/modulo/api/routes.py", Path.cwd(), "backend") == (
        "backend/src/modulo/api/routes.py"
    )


def test_normalize_report_path_keeps_already_prefixed_backend_path():
    assert gate.normalize_report_path("backend/src/modulo/api/routes.py", Path.cwd(), "backend") == (
        "backend/src/modulo/api/routes.py"
    )


def test_normalize_report_path_prefixes_frontend_src():
    assert gate.normalize_report_path("src/stores/auth.ts", Path.cwd(), "frontend") == ("frontend/src/stores/auth.ts")


def test_normalize_report_path_relativizes_absolute_path_inside_repo(tmp_path):
    absolute = tmp_path / "backend" / "src" / "modulo" / "routes.py"
    assert gate.normalize_report_path(str(absolute), tmp_path, "backend") == ("backend/src/modulo/routes.py")


def test_normalize_report_path_handles_windows_separators(tmp_path):
    assert gate.normalize_report_path("src\\modulo\\routes.py", tmp_path, "backend") == ("backend/src/modulo/routes.py")


def test_normalize_report_path_strips_dot_slash():
    assert gate.normalize_report_path("./src/stores/auth.ts", Path.cwd(), "frontend") == ("frontend/src/stores/auth.ts")


# ---------------------------------------------------------------------------
# parse_cobertura
# ---------------------------------------------------------------------------


def test_parse_cobertura_counts_line_hits_and_normalizes_filename(tmp_path):
    xml_path = tmp_path / "backend" / "coverage.xml"
    _write_cobertura(
        xml_path,
        [
            _class_xml(
                "src/modulo/api/routes.py",
                [
                    _line_xml(1, 1),
                    _line_xml(0, 2),
                    _line_xml(1, 3),
                    _line_xml(1, 4),
                    _line_xml(0, 5),
                ],
            )
        ],
    )
    files = gate.parse_cobertura(xml_path, tmp_path)
    entry = files["backend/src/modulo/api/routes.py"]
    assert entry.lines_valid == 5
    assert entry.lines_covered == 3


def test_parse_cobertura_aggregates_branch_condition_totals(tmp_path):
    xml_path = tmp_path / "backend" / "coverage.xml"
    _write_cobertura(
        xml_path,
        [
            _class_xml(
                "src/modulo/api/routes.py",
                [
                    _line_xml(1, 1),
                    _line_xml(1, 2, "50% (1/2)"),
                    _line_xml(1, 3, "100% (2/2)"),
                ],
            )
        ],
    )
    files = gate.parse_cobertura(xml_path, tmp_path)
    entry = files["backend/src/modulo/api/routes.py"]
    assert entry.conditions_valid == 4
    assert entry.conditions_covered == 3


def test_parse_cobertura_ignores_lines_without_condition_attribute_for_branches(tmp_path):
    xml_path = tmp_path / "backend" / "coverage.xml"
    _write_cobertura(xml_path, [_class_xml("src/modulo/api/routes.py", [_line_xml(1, 1), _line_xml(0, 2)])])
    files = gate.parse_cobertura(xml_path, tmp_path)
    entry = files["backend/src/modulo/api/routes.py"]
    assert entry.conditions_valid == 0
    assert entry.conditions_covered == 0


def test_parse_cobertura_merges_duplicate_class_entries_per_file(tmp_path):
    xml_path = tmp_path / "backend" / "coverage.xml"
    _write_cobertura(
        xml_path,
        [
            _class_xml("src/modulo/api/routes.py", [_line_xml(1, 1)]),
            _class_xml("src/modulo/api/routes.py", [_line_xml(0, 2)]),
        ],
    )
    files = gate.parse_cobertura(xml_path, tmp_path)
    entry = files["backend/src/modulo/api/routes.py"]
    assert entry.lines_valid == 2
    assert entry.lines_covered == 1


def test_parse_cobertura_skips_class_without_filename(tmp_path):
    xml_path = tmp_path / "backend" / "coverage.xml"
    _write_cobertura(xml_path, [_class_xml("src/modulo/api/routes.py", [_line_xml(1, 1)]), "<class/>"])
    files = gate.parse_cobertura(xml_path, tmp_path)
    assert list(files) == ["backend/src/modulo/api/routes.py"]


# ---------------------------------------------------------------------------
# parse_lcov
# ---------------------------------------------------------------------------


def test_parse_lcov_reads_sf_lf_lh_and_normalizes_path(tmp_path):
    lcov_path = tmp_path / "frontend" / "coverage" / "lcov.info"
    _write_lcov(lcov_path, [_lcov_record("src/stores/auth.ts", 10, 8)])
    files = gate.parse_lcov(lcov_path, tmp_path)
    entry = files["frontend/src/stores/auth.ts"]
    assert entry.lines_valid == 10
    assert entry.lines_covered == 8


def test_parse_lcov_merges_duplicate_records_for_same_file(tmp_path):
    lcov_path = tmp_path / "frontend" / "coverage" / "lcov.info"
    _write_lcov(
        lcov_path,
        [_lcov_record("src/stores/auth.ts", 10, 8), _lcov_record("src/stores/auth.ts", 5, 5)],
    )
    files = gate.parse_lcov(lcov_path, tmp_path)
    entry = files["frontend/src/stores/auth.ts"]
    assert entry.lines_valid == 15
    assert entry.lines_covered == 13


def test_parse_lcov_ignores_stray_lf_lh_outside_record(tmp_path):
    lcov_path = tmp_path / "frontend" / "coverage" / "lcov.info"
    _write_lcov(lcov_path, ["LF:99", "LH:99", _lcov_record("src/utils/math.ts", 4, 4)])
    files = gate.parse_lcov(lcov_path, tmp_path)
    assert list(files) == ["frontend/src/utils/math.ts"]
    assert files["frontend/src/utils/math.ts"].lines_valid == 4


# ---------------------------------------------------------------------------
# unmeasured-file handling
# ---------------------------------------------------------------------------


def test_count_non_blank_lines_counts_only_non_blank(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("import os\n\n\ndef run():\n    return 1\n\n", encoding="utf-8")
    assert gate.count_non_blank_lines(target) == 3


def test_collect_rows_counts_unmeasured_file_as_zero_percent(tmp_path, capsys):
    source = tmp_path / "backend" / "src" / "modulo" / "brand_new.py"
    source.parent.mkdir(parents=True)
    source.write_text("a = 1\n\nb = 2\nc = 3\n", encoding="utf-8")
    backend_report = {"backend/src/modulo/measured.py": gate.FileCoverage(lines_valid=10, lines_covered=9)}

    rows = gate.collect_rows(
        ["backend/src/modulo/brand_new.py", "backend/src/modulo/measured.py"],
        backend_report,
        {},
        tmp_path,
    )

    assert rows == [
        gate.GateRow("backend/src/modulo/brand_new.py", "backend", 3, 0, "unmeasured"),
        gate.GateRow("backend/src/modulo/measured.py", "backend", 10, 9, "report"),
    ]
    out = capsys.readouterr().out
    assert "brand_new.py" in out
    assert "absent from coverage report" in out


def test_collect_rows_skips_changed_file_missing_from_tree(tmp_path, capsys):
    rows = gate.collect_rows(
        ["backend/src/modulo/deleted.py"],
        {"backend/src/modulo/other.py": gate.FileCoverage(lines_valid=5, lines_covered=5)},
        {},
        tmp_path,
    )
    assert rows == []
    out = capsys.readouterr().out
    assert "not present in tree" in out


def test_collect_rows_skips_language_without_report(tmp_path):
    rows = gate.collect_rows(
        ["backend/src/modulo/routes.py", "frontend/src/stores/auth.ts"],
        None,
        None,
        tmp_path,
    )
    assert rows == []


def test_collect_rows_routes_frontend_files_to_lcov_report(tmp_path):
    source = tmp_path / "frontend" / "src" / "stores" / "auth.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const a = 1\nexport const b = 2\n", encoding="utf-8")
    rows = gate.collect_rows(["frontend/src/stores/auth.ts"], {}, {}, tmp_path)
    assert rows == [gate.GateRow("frontend/src/stores/auth.ts", "frontend", 2, 0, "unmeasured")]


# ---------------------------------------------------------------------------
# branch totals / formatting
# ---------------------------------------------------------------------------


def test_branch_totals_aggregates_only_measured_paths():
    report = {
        "backend/src/modulo/a.py": gate.FileCoverage(conditions_valid=4, conditions_covered=3),
        "backend/src/modulo/b.py": gate.FileCoverage(conditions_valid=2, conditions_covered=1),
        "backend/src/modulo/c.py": gate.FileCoverage(conditions_valid=8, conditions_covered=8),
    }
    valid, covered = gate.branch_totals(report, ["backend/src/modulo/a.py", "backend/src/modulo/c.py"])
    assert valid == 12
    assert covered == 11


def test_format_pct_handles_zero_valid():
    assert gate.format_pct(0, 0) == "n/a"


def test_format_pct_rounds_to_one_decimal():
    assert gate.format_pct(5, 6) == "83.3%"


def test_render_table_includes_rows_and_header():
    rows = [gate.GateRow("backend/src/modulo/a.py", "backend", 10, 9, "report")]
    table = gate.render_table(rows)
    assert "| File | Language | Valid lines | Covered | Coverage | Source |" in table
    assert "backend/src/modulo/a.py" in table
    assert "90.0%" in table


def test_emit_table_writes_to_github_step_summary(tmp_path, monkeypatch):
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    gate.emit_table([gate.GateRow("backend/src/modulo/a.py", "backend", 4, 4, "report")])
    content = summary.read_text(encoding="utf-8")
    assert "backend/src/modulo/a.py" in content
    assert "100.0%" in content


def test_emit_table_prints_to_stdout_without_step_summary(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    gate.emit_table([gate.GateRow("backend/src/modulo/a.py", "backend", 4, 4, "report")])
    out = capsys.readouterr().out
    assert "backend/src/modulo/a.py" in out


# ---------------------------------------------------------------------------
# main() verdicts (git helpers patched)
# ---------------------------------------------------------------------------


def _patch_git(changed: list[str]):
    return (
        patch.object(gate, "get_merge_base", return_value="abc123"),
        patch.object(gate, "get_changed_files", return_value=changed),
    )


def test_main_passes_above_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_cobertura(
        tmp_path / "backend" / "coverage.xml",
        [_class_xml("src/modulo/api/routes.py", [_line_xml(1, n) for n in range(1, 11)])],
    )
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [_lcov_record("src/stores/auth.ts", 10, 10)])
    changed = ["backend/src/modulo/api/routes.py", "frontend/src/stores/auth.ts"]
    patch_merge_base, patch_changed = _patch_git(changed)
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 0


def test_main_fails_below_threshold_with_error_annotation(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    backend_lines = [_line_xml(1, n) for n in range(1, 6)] + [_line_xml(0, n) for n in range(6, 11)]
    _write_cobertura(
        tmp_path / "backend" / "coverage.xml",
        [_class_xml("src/modulo/api/routes.py", backend_lines)],
    )
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [_lcov_record("src/stores/auth.ts", 10, 10)])
    changed = ["backend/src/modulo/api/routes.py", "frontend/src/stores/auth.ts"]
    patch_merge_base, patch_changed = _patch_git(changed)
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "::error::New-code coverage gate failed: backend 50.0% / frontend 100.0% (threshold 90%)" in out


def test_main_boundary_exactly_at_threshold_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_cobertura(
        tmp_path / "backend" / "coverage.xml",
        [_class_xml("src/modulo/api/routes.py", [_line_xml(1, n) for n in range(1, 10)] + [_line_xml(0, 10)])],
    )
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [_lcov_record("src/stores/auth.ts", 10, 10)])
    changed = ["backend/src/modulo/api/routes.py", "frontend/src/stores/auth.ts"]
    patch_merge_base, patch_changed = _patch_git(changed)
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 0


def test_main_trivial_diff_is_exempt(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_cobertura(
        tmp_path / "backend" / "coverage.xml",
        [_class_xml("src/modulo/api/routes.py", [_line_xml(0, 1), _line_xml(0, 2), _line_xml(0, 3)])],
    )
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [])
    changed = ["backend/src/modulo/api/routes.py"]
    patch_merge_base, patch_changed = _patch_git(changed)
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert "trivial diff" in out


def test_main_unmeasured_brand_new_file_fails_gate(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_cobertura(
        tmp_path / "backend" / "coverage.xml",
        [_class_xml("src/modulo/api/routes.py", [_line_xml(1, n) for n in range(1, 11)])],
    )
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [_lcov_record("src/stores/auth.ts", 10, 10)])
    untested = tmp_path / "backend" / "src" / "modulo" / "brand_new.py"
    untested.parent.mkdir(parents=True)
    untested.write_text("a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n\n# comment\n", encoding="utf-8")
    changed = [
        "backend/src/modulo/api/routes.py",
        "frontend/src/stores/auth.ts",
        "backend/src/modulo/brand_new.py",
    ]
    patch_merge_base, patch_changed = _patch_git(changed)
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "brand_new.py" in out
    assert "absent from coverage report" in out


def test_main_missing_backend_report_is_exempt_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [_lcov_record("src/stores/auth.ts", 10, 10)])
    changed = ["frontend/src/stores/auth.ts"]
    patch_merge_base, patch_changed = _patch_git(changed)
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert "::warning::Backend coverage report not found" in out
    assert "EXEMPT" in out


def test_main_git_failure_returns_two(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with patch.object(gate, "get_merge_base", side_effect=gate.GitError("merge-base blew up")):
        assert gate.main([]) == 2
    out = capsys.readouterr().out
    assert "::error::New-code coverage gate: git failure: merge-base blew up" in out


def test_main_no_production_files_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    patch_merge_base, patch_changed = _patch_git(["README.md", "backend/tests/unit/test_x.py"])
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert "no changed production files" in out


def test_main_malformed_coverage_xml_returns_two(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    broken = tmp_path / "backend" / "coverage.xml"
    broken.parent.mkdir(parents=True)
    broken.write_text("<coverage><unclosed>", encoding="utf-8")
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [])
    patch_merge_base, patch_changed = _patch_git(["backend/src/modulo/routes.py"])
    with patch_merge_base, patch_changed:
        assert gate.main([]) == 2
    out = capsys.readouterr().out
    assert "could not read coverage reports" in out


def test_main_custom_threshold_and_min_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_cobertura(
        tmp_path / "backend" / "coverage.xml",
        [_class_xml("src/modulo/api/routes.py", [_line_xml(1, 1), _line_xml(0, 2), _line_xml(0, 3)])],
    )
    _write_lcov(tmp_path / "frontend" / "coverage" / "lcov.info", [_lcov_record("src/stores/auth.ts", 10, 10)])
    changed = ["backend/src/modulo/api/routes.py", "frontend/src/stores/auth.ts"]
    patch_merge_base, patch_changed = _patch_git(changed)
    with patch_merge_base, patch_changed:
        assert gate.main(["--threshold", "30", "--min-lines", "10"]) == 0


def test_parse_args_defaults():
    args = gate.parse_args([])
    assert args.base == "origin/main"
    assert args.coverage_xml == "backend/coverage.xml"
    assert args.lcov == "frontend/coverage/lcov.info"
    assert args.threshold == 90.0
    assert args.min_lines == 10


def test_parse_args_overrides():
    args = gate.parse_args(
        [
            "--base",
            "origin/feature",
            "--coverage-xml",
            "custom.xml",
            "--lcov",
            "custom.info",
            "--threshold",
            "80",
            "--min-lines",
            "25",
        ]
    )
    assert args.base == "origin/feature"
    assert args.coverage_xml == "custom.xml"
    assert args.lcov == "custom.info"
    assert args.threshold == 80.0
    assert args.min_lines == 25
