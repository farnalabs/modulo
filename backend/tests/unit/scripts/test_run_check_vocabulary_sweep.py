"""Unit tests for scripts/run_check_vocabulary_sweep.py (FAR-588, ADR 029).

The sweep forbids the retired space/hyphen compounds "sandbox agent" and
"external agent" (case-insensitive) in user-facing surfaces while allowlisting
the frozen underscore identifiers. These tests drive discovery and ``main``
against a temp tree with a re-pointed ``SCAN_SURFACE``/``REPO_ROOT``.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_vocabulary_sweep.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_vocabulary_sweep.py)")

_loader = SourceFileLoader("run_check_vocabulary_sweep", str(script_path))
mod = module_from_spec(spec_from_loader("run_check_vocabulary_sweep", _loader))
_loader.exec_module(mod)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _point_at(tmp_path: Path, monkeypatch) -> Path:
    """Build the scanned surfaces under ``tmp_path`` and re-point the module."""
    locales = tmp_path / "frontend" / "src" / "locales"
    views = tmp_path / "frontend" / "src" / "views"
    components = tmp_path / "frontend" / "src" / "components"
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "SCAN_SURFACE", [locales, views, components])
    return tmp_path


def test_scan_files_locales_js_only(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/locales/en-US.js", "export default {};\n")
    _write(tmp_path / "frontend/src/locales/ignored.ts", "export default {};\n")
    _write(tmp_path / "frontend/src/locales/nested/deep.js", "export default {};\n")

    found = mod._scan_files()

    assert [path.name for path in found] == ["en-US.js"]


def test_scan_files_vue_recursively_everywhere_else(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/views/Home.vue", "<template />\n")
    _write(tmp_path / "frontend/src/views/sub/Deep.vue", "<template />\n")
    _write(tmp_path / "frontend/src/views/ignored.js", "export default {};\n")
    _write(tmp_path / "frontend/src/components/Widget.vue", "<template />\n")

    names = sorted(path.name for path in mod._scan_files())

    assert names == ["Deep.vue", "Home.vue", "Widget.vue"]


def test_main_clean_surface_returns_zero(tmp_path, monkeypatch, capsys):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/locales/en-US.js", "export default {};\n")
    _write(tmp_path / "frontend/src/views/Home.vue", "<template><p>Runner</p></template>\n")

    assert mod.main() == 0
    assert "clean" in capsys.readouterr().err


def test_main_flags_space_compound_in_locale(tmp_path, monkeypatch, capsys):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/locales/en-US.js", "export default {\n  a: 'sandbox agent',\n};\n")

    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "sandbox agent" in err
    assert "en-US.js:2" in err


def test_main_flags_hyphen_external_compound_in_nested_vue(tmp_path, monkeypatch, capsys):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/views/sub/Deep.vue", "<template><p>External-Agent</p></template>\n")

    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "External-Agent" in err
    assert "Deep.vue:1" in err


def test_main_allowlists_frozen_underscore_identifiers(tmp_path, monkeypatch, capsys):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/locales/en-US.js", "export default { sandbox_agent: 'Runner' };\n")
    _write(tmp_path / "frontend/src/views/Home.vue", "<template><p>sandbox_agent</p></template>\n")

    assert mod.main() == 0
    assert "clean" in capsys.readouterr().err


def test_main_case_insensitive_match(tmp_path, monkeypatch, capsys):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/components/Widget.vue", "<template><p>SANDBOX AGENT</p></template>\n")

    assert mod.main() == 1
    assert "SANDBOX AGENT" in capsys.readouterr().err


def test_main_ignores_non_scanned_extensions(tmp_path, monkeypatch, capsys):
    _point_at(tmp_path, monkeypatch)
    _write(tmp_path / "frontend/src/views/notes.ts", "export const retired = 'sandbox agent';\n")

    assert mod.main() == 0
    assert "clean" in capsys.readouterr().err
