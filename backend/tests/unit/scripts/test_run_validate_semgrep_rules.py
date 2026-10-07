"""Unit tests for scripts/run_validate_semgrep_rules.py (FAR semgrep rule-shape gate).

The gate validates rule-FILE shape before commit: no UTF-8 BOM, and a
top-level ``rules:`` key as the first meaningful line. These tests pin the
helpers directly and drive ``main`` end to end against a temp repo tree by
re-pointing the module's ``__file__`` (``main`` derives the repo root from it).
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_validate_semgrep_rules.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_validate_semgrep_rules.py)")

_loader = SourceFileLoader("run_validate_semgrep_rules", str(script_path))
mod = module_from_spec(spec_from_loader("run_validate_semgrep_rules", _loader))
_loader.exec_module(mod)

_BOM = b"\xef\xbb\xbf"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _point_main_at(tmp_path: Path, monkeypatch) -> None:
    """Re-point ``main`` at ``tmp_path`` as the repo root via the module's
    ``__file__`` (``main`` computes ``Path(__file__).parent.parent``)."""
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "scripts" / "run_validate_semgrep_rules.py"))


def test_collect_rule_files_recurses_and_sorts(tmp_path):
    _write(tmp_path / "b.yaml", b"rules:\n")
    _write(tmp_path / "a.yml", b"rules:\n")
    _write(tmp_path / "nested" / "c.yml", b"rules:\n")
    _write(tmp_path / "ignored.json", b"{}")

    found = mod.collect_rule_files(tmp_path)

    assert [path.name for path in found] == ["a.yml", "b.yaml", "c.yml"]


def test_collect_rule_files_empty_dir(tmp_path):
    assert not mod.collect_rule_files(tmp_path)


def test_collect_rule_files_deduplicates_across_patterns(tmp_path):
    _write(tmp_path / "only.yml", b"rules:\n")

    found = mod.collect_rule_files(tmp_path)

    assert found == [tmp_path / "only.yml"]


def test_has_bom_detects_prefix(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, _BOM + b"rules:\n")

    assert mod.has_bom(target) is True


def test_has_bom_false_for_clean_file(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, b"rules:\n")

    assert mod.has_bom(target) is False


def test_has_top_level_rules_key_allows_leading_comments_and_blanks(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, b"\n# a comment\n\nrules:\n  - id: x\n")

    assert mod.has_top_level_rules_key(target) is True


def test_has_top_level_rules_key_rejects_other_first_key(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, b"other: 1\nrules:\n")

    assert mod.has_top_level_rules_key(target) is False


def test_has_top_level_rules_key_rejects_inline_value(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, b"rules: []\n")

    assert mod.has_top_level_rules_key(target) is False


def test_has_top_level_rules_key_rejects_bom_prefixed_key(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, _BOM + b"rules:\n")

    assert mod.has_top_level_rules_key(target) is False


def test_check_file_clean_returns_none(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, b"rules:\n  - id: x\n")

    assert mod.check_file(target) is None


def test_check_file_flags_bom(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, _BOM + b"rules:\n")

    reason = mod.check_file(target)

    assert reason is not None
    assert "BOM" in reason


def test_check_file_flags_missing_rules_key(tmp_path):
    target = tmp_path / "rules.yml"
    _write(target, b"other: 1\n")

    reason = mod.check_file(target)

    assert reason is not None
    assert "rules" in reason


def test_main_fails_when_semgrep_dir_missing(tmp_path, monkeypatch, capsys):
    _point_main_at(tmp_path, monkeypatch)

    assert mod.main() == 1
    assert ".semgrep" in capsys.readouterr().out


def test_main_fails_when_no_rule_files_found(tmp_path, monkeypatch, capsys):
    (tmp_path / ".semgrep").mkdir()
    _point_main_at(tmp_path, monkeypatch)

    assert mod.main() == 1
    assert "no .yml/.yaml rule files" in capsys.readouterr().out


def test_main_passes_clean_tree(tmp_path, monkeypatch, capsys):
    _write(tmp_path / ".semgrep" / "rules.yml", b"rules:\n  - id: x\n")
    _point_main_at(tmp_path, monkeypatch)

    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "checked=1 failed=0" in out


def test_main_fails_and_reports_bom_file(tmp_path, monkeypatch, capsys):
    _write(tmp_path / ".semgrep" / "rules.yml", _BOM + b"rules:\n")
    _point_main_at(tmp_path, monkeypatch)

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "checked=1 failed=1" in out
    assert "BOM" in out
