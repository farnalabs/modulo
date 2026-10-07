"""Unit tests for scripts/run_validate_translations.py (nav key parity gate).

The gate asserts every ``components.SidebarNav.item_*`` label referenced in
``navigation.ts`` has a matching ``item_*`` entry in ``en-US.js``. These tests
drive the regex extraction and the ``--diff-range`` skip path against a temp
tree by re-pointing ``REPO_ROOT`` and substituting ``subprocess.run``.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_validate_translations.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_validate_translations.py)")

_loader = SourceFileLoader("run_validate_translations", str(script_path))
mod = module_from_spec(spec_from_loader("run_validate_translations", _loader))
_loader.exec_module(mod)


class _Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_repo(tmp_path: Path, monkeypatch, nav: str, locale: str) -> None:
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    _write(tmp_path / mod._NAV_PATH, nav)
    _write(tmp_path / mod._LOCALE_PATH, locale)


def test_main_ok_when_all_keys_defined(tmp_path, monkeypatch, capsys):
    _seed_repo(
        tmp_path,
        monkeypatch,
        "nav = [{ label: 'components.SidebarNav.item_home' }];\n",
        'export default { "item_home": "Home" };\n',
    )

    assert mod._main([]) == 0
    assert "all 1 sidebar nav translation keys present" in capsys.readouterr().err


def test_main_flags_missing_key(tmp_path, monkeypatch, capsys):
    _seed_repo(
        tmp_path,
        monkeypatch,
        "nav = [{ label: 'components.SidebarNav.item_home' }, { label: 'components.SidebarNav.item_runs' }];\n",
        'export default { "item_home": "Home" };\n',
    )

    assert mod._main([]) == 1
    err = capsys.readouterr().err
    assert "components.SidebarNav.item_runs" in err
    assert "1 key(s) referenced" in err


def test_main_ignores_non_item_nav_labels(tmp_path, monkeypatch, capsys):
    _seed_repo(
        tmp_path,
        monkeypatch,
        "nav = [{ label: 'components.SidebarNav.brand' }];\n",
        'export default { "item_home": "Home" };\n',
    )

    assert mod._main([]) == 0
    assert "all 0 sidebar nav translation keys present" in capsys.readouterr().err


def test_main_returns_zero_when_nav_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))

    assert mod._main([]) == 0
    assert "not found - skipping" in capsys.readouterr().err


def test_main_returns_zero_when_locale_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "REPO_ROOT", str(tmp_path))
    _write(tmp_path / mod._NAV_PATH, "nav = [{ label: 'components.SidebarNav.item_home' }];\n")

    assert mod._main([]) == 0
    assert "not found - skipping" in capsys.readouterr().err


def test_diff_range_skips_when_neither_file_changed(tmp_path, monkeypatch, capsys):
    _seed_repo(tmp_path, monkeypatch, "", "")
    monkeypatch.setattr(mod, "_files_changed", lambda diff_range: False)

    assert mod._main(["--diff-range", "main...HEAD"]) == 0
    assert "skipping" in capsys.readouterr().err


def test_diff_range_proceeds_when_file_changed(tmp_path, monkeypatch):
    _seed_repo(
        tmp_path,
        monkeypatch,
        "nav = [{ label: 'components.SidebarNav.item_home' }];\n",
        'export default { "item_home": "Home" };\n',
    )
    monkeypatch.setattr(mod, "_files_changed", lambda diff_range: True)

    assert mod._main(["--diff-range", "main...HEAD"]) == 0


def test_files_changed_true_when_nav_in_diff(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: _Completed(stdout=f"{mod._NAV_PATH}\nbackend/other.py\n"),
    )

    assert mod._files_changed("main...HEAD") is True


def test_files_changed_false_when_unrelated(monkeypatch):
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: _Completed(stdout="backend/other.py\nfrontend/package.json\n"),
    )

    assert mod._files_changed("main...HEAD") is False


def test_invalid_diff_range_exits_nonzero():
    with pytest.raises(SystemExit) as exc_info:
        mod._files_changed("bad range")

    assert exc_info.value.code == 1
