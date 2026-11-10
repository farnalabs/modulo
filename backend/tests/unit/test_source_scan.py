"""Regression tests for the shared whole-tree source scanner.

The vulture dead-code gate used to write a transient module into
``backend/src/farna`` and unlink it again. The probe is now staged in pytest's
``tmp_path`` — outside the measured source tree and outside the repo — and passed
to ``scripts/run_vulture.py`` as an extra scan target (FAR-2334), so that
particular ``FileNotFoundError`` race can no longer occur and no fix is needed
for it. The scanner still skips files that vanish mid-scan, because any
transient file under a scanned root (a fixture, an editor swap file, concurrent
cleanup) has the same race: a scan in another xdist worker can list a path during
its directory walk and read it after a fixture has removed it.
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
