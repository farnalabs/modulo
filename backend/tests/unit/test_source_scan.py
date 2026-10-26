"""Regression tests for the shared whole-tree source scanner.

The vulture dead-code gate writes a transient module into
``backend/src/modulo`` and unlinks it again. A scan in another xdist worker can
list that path during its directory walk and read it after the unlink, so a
static scan must skip files that vanish mid-scan rather than raise
``FileNotFoundError``.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit._source_scan import iter_source_text


def test_iter_source_text_yields_python_files_with_text(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("not python\n", encoding="utf-8")
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "c.py").write_text("C = 3\n", encoding="utf-8")

    assert list(iter_source_text(tmp_path)) == [(tmp_path / "a.py", "A = 1\n")]


def test_iter_source_text_skips_a_file_removed_mid_scan(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("B = 2\n", encoding="utf-8")

    files = iter_source_text(tmp_path)
    assert next(files) == (tmp_path / "a.py", "A = 1\n")

    (tmp_path / "b.py").unlink()
    assert not list(files)
