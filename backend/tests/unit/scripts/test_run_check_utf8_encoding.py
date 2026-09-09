"""Unit tests for the new ``_check_file`` helper in scripts/run_check_utf8_encoding.py."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_utf8_encoding.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_utf8_encoding.py)")

_loader = SourceFileLoader("run_check_utf8_encoding", str(script_path))
mod = module_from_spec(spec_from_loader("run_check_utf8_encoding", _loader))
_loader.exec_module(mod)


def test_check_file_unsupported_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    f = tmp_path / "note.txt"
    f.write_bytes(b"hello")
    assert mod._check_file("note.txt", fix=False) is False


def test_check_file_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    assert mod._check_file("missing.py", fix=False) is False


def test_check_file_utf8_bom_workflow_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_bytes(mod._UTF8_BOM + b"name: ci\n")
    assert mod._check_file(".github/workflows/ci.yml", fix=False) is True


def test_check_file_utf8_bom_workflow_fixed(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_bytes(mod._UTF8_BOM + b"name: ci\n")
    assert mod._check_file(".github/workflows/ci.yml", fix=True) is True
    # BOM removed after fix.
    assert wf.read_bytes() == b"name: ci\n"


def test_check_file_utf8_bom_non_workflow_non_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    f = tmp_path / "mod.py"
    f.write_bytes(mod._UTF8_BOM + b"x = 1\n")
    assert mod._check_file("mod.py", fix=False) is False


def test_check_file_utf8_bom_non_workflow_fixed(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    f = tmp_path / "mod.py"
    f.write_bytes(mod._UTF8_BOM + b"x = 1\n")
    assert mod._check_file("mod.py", fix=True) is False
    assert f.read_bytes() == b"x = 1\n"


def test_check_file_utf16_bom_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    f = tmp_path / "data.json"
    f.write_bytes(mod._UTF16_LE_BOM + "{}".encode("utf-16-le"))
    assert mod._check_file("data.json", fix=False) is True


def test_check_file_clean_file_not_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    f = tmp_path / "clean.py"
    f.write_bytes(b"x = 1\n")
    assert mod._check_file("clean.py", fix=False) is False
