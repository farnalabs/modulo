"""Unit tests for scripts/run_check_c1_chars.py (FAR-1350).

The script self-discovers its targets (pre-commit runs it with
``pass_filenames: false``), so these tests pin the discovery scope
(recursive, *.yml and *.yaml) and the scan behaviour end to end.
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_c1_chars.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_c1_chars.py)")

_loader = SourceFileLoader("run_check_c1_chars", str(script_path))
mod = module_from_spec(spec_from_loader("run_check_c1_chars", _loader))
_loader.exec_module(mod)

_NEL = b"\xc2\x85"  # U+0085 (NEL), a C1 control char, UTF-8 encoded


def _use_tmp_workflows_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", workflows)
    return workflows


def test_no_workflow_files_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_tmp_workflows_dir(tmp_path, monkeypatch)
    assert mod.main() == 0


def test_clean_workflow_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflows = _use_tmp_workflows_dir(tmp_path, monkeypatch)
    (workflows / "ci.yml").write_bytes(b"name: ci\non: push\n")
    assert mod.main() == 0


def test_c1_char_in_yml_workflow_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    workflows = _use_tmp_workflows_dir(tmp_path, monkeypatch)
    (workflows / "ci.yml").write_bytes(b"name: ci\n" + _NEL + b"\n")
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "U+0085" in err
    assert "ci.yml" in err


def test_c1_char_in_yaml_workflow_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    workflows = _use_tmp_workflows_dir(tmp_path, monkeypatch)
    (workflows / "ci.yaml").write_bytes(b"name: ci\n" + _NEL + b"\n")
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "U+0085" in err
    assert "ci.yaml" in err


def test_nested_workflow_is_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    workflows = _use_tmp_workflows_dir(tmp_path, monkeypatch)
    subdir = workflows / "subdir"
    subdir.mkdir()
    (subdir / "deploy.yml").write_bytes(b"name: deploy\n" + _NEL + b"\n")
    assert mod.main() == 1
    assert "deploy.yml" in capsys.readouterr().err


def test_utf8_bom_in_workflow_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    workflows = _use_tmp_workflows_dir(tmp_path, monkeypatch)
    (workflows / "ci.yml").write_bytes(b"\xef\xbb\xbfname: ci\n")
    assert mod.main() == 1
    assert "BOM" in capsys.readouterr().err


def test_fix_removes_c1_char(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflows = _use_tmp_workflows_dir(tmp_path, monkeypatch)
    target = workflows / "ci.yml"
    target.write_bytes(b"name: ci\n" + _NEL + b"\n")
    monkeypatch.setattr(sys, "argv", ["run_check_c1_chars.py", "--fix"])
    assert mod.main() == 1
    assert target.read_bytes() == b"name: ci\n\n"
