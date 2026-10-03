#!/usr/bin/env python3
"""Unit tests for scripts/run_check_c1_chars.py.

Run from the repo root:
    uv run --project backend pytest tests/unit/scripts/test_run_check_c1_chars.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the scripts directory is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_check_c1_chars as c1_module  # noqa: E402


def _write_workflow(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _patch_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(c1_module, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_check_c1_chars.py"])


def _patch_env_with_fix(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(c1_module, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_check_c1_chars.py", "--fix"])


class TestNoWorkflowFiles:
    def test_empty_workflows_dir_is_a_pass(self, tmp_path, monkeypatch, capsys):
        _patch_env(tmp_path, monkeypatch)
        assert c1_module.main() == 0
        captured = capsys.readouterr()
        assert "No C1 control characters or BOMs found" in captured.err


class TestCleanFiles:
    def test_ascii_workflow_passes(self, tmp_path, monkeypatch, capsys):
        _write_workflow(tmp_path, "ci.yml", b"name: ci\non: [push]\n")
        _patch_env(tmp_path, monkeypatch)
        assert c1_module.main() == 0
        captured = capsys.readouterr()
        assert "No C1 control characters or BOMs found" in captured.err

    def test_allowlisted_unicode_is_not_flagged(self, tmp_path, monkeypatch, capsys):
        # U+2014 (em dash) is on the allowlist.
        _write_workflow(tmp_path, "docs.yml", "name: docs \u2014 build\n".encode("utf-8"))
        _patch_env(tmp_path, monkeypatch)
        assert c1_module.main() == 0
        captured = capsys.readouterr()
        assert "No C1 control characters or BOMs found" in captured.err


class TestBomDetection:
    def test_bom_is_reported(self, tmp_path, monkeypatch, capsys):
        path = _write_workflow(tmp_path, "ci.yml", b"\xef\xbb\xbfname: ci\n")
        _patch_env(tmp_path, monkeypatch)
        assert c1_module.main() == 1
        captured = capsys.readouterr()
        assert "UTF-8 BOM found in ci.yml" in captured.err
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_bom_is_removed_with_fix(self, tmp_path, monkeypatch, capsys):
        path = _write_workflow(tmp_path, "ci.yml", b"\xef\xbb\xbfname: ci\n")
        _patch_env_with_fix(tmp_path, monkeypatch)
        assert c1_module.main() == 1
        captured = capsys.readouterr()
        assert "Fixed: removed UTF-8 BOM" in captured.err
        assert path.read_bytes() == b"name: ci\n"


class TestC1ControlCharacters:
    def test_c1_character_is_reported(self, tmp_path, monkeypatch, capsys):
        # U+0085 (NEL) is a C1 control character.
        _write_workflow(tmp_path, "ci.yml", "name: ci\u0085\n".encode("utf-8"))
        _patch_env(tmp_path, monkeypatch)
        assert c1_module.main() == 1
        captured = capsys.readouterr()
        assert "C1 control char U+0085 found in ci.yml" in captured.err

    def test_c1_character_is_removed_with_fix(self, tmp_path, monkeypatch, capsys):
        path = _write_workflow(tmp_path, "ci.yml", "name: ci\u0085\n".encode("utf-8"))
        _patch_env_with_fix(tmp_path, monkeypatch)
        assert c1_module.main() == 1
        captured = capsys.readouterr()
        assert "Fixed: removed C1 char U+0085" in captured.err
        assert path.read_text(encoding="utf-8") == "name: ci\n"


class TestNonAsciiDetection:
    def test_non_ascii_character_is_reported_with_location(self, tmp_path, monkeypatch, capsys):
        # U+00E9 (é) is non-ASCII and not on the allowlist.
        _write_workflow(tmp_path, "ci.yml", "name: caf\u00e9\n".encode("utf-8"))
        _patch_env(tmp_path, monkeypatch)
        assert c1_module.main() == 1
        captured = capsys.readouterr()
        assert "Non-ASCII char U+00E9 at ci.yml:1:10" in captured.err

    def test_multiple_files_report_each_offender(self, tmp_path, monkeypatch, capsys):
        _write_workflow(tmp_path, "a.yml", "x: \u00e9\n".encode("utf-8"))
        _write_workflow(tmp_path, "b.yaml", "x: \u00e9\n".encode("utf-8"))
        _patch_env(tmp_path, monkeypatch)
        assert c1_module.main() == 1
        captured = capsys.readouterr()
        assert "at a.yml:1:4" in captured.err
        assert "at b.yaml:1:4" in captured.err
