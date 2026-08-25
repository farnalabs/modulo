"""Unit tests for bump-version.py — semver bump across pyproject.toml and package.json."""

from __future__ import annotations

import shutil
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "bump-version.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/bump-version.py)")

_bv_loader = SourceFileLoader("bump_version", str(script_path))
bv = module_from_spec(spec_from_loader("bump_version", _bv_loader))
_bv_loader.exec_module(bv)


# ---------------------------------------------------------------------------
# bump
# ---------------------------------------------------------------------------


def test_bump_patch():
    assert bv.bump("1.2.3", "patch") == "1.2.4"


def test_bump_minor():
    assert bv.bump("1.2.3", "minor") == "1.3.0"


def test_bump_major():
    assert bv.bump("1.2.3", "major") == "2.0.0"


def test_bump_resets_lower_parts():
    assert bv.bump("2.9.9", "minor") == "2.10.0"
    assert bv.bump("2.9.9", "major") == "3.0.0"


def test_bump_unknown_part_raises():
    with pytest.raises(ValueError, match="Unknown part"):
        bv.bump("1.2.3", "nonsense")


def test_bump_invalid_version_raises():
    with pytest.raises(ValueError, match="not enough values to unpack"):
        bv.bump("1.2", "patch")
    with pytest.raises(ValueError, match="invalid literal for int"):
        bv.bump("not-a-version", "patch")


# ---------------------------------------------------------------------------
# read_version / write_version
# ---------------------------------------------------------------------------


def test_read_version_toml(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "modulo"\nversion = "1.2.3"\n')
    assert bv.read_version(path) == "1.2.3"


def test_read_version_json(tmp_path):
    path = tmp_path / "package.json"
    path.write_text('{"name": "modulo", "version": "4.5.6"}\n')
    assert bv.read_version(path) == "4.5.6"


def test_read_version_missing_returns_none(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "modulo"\n')
    assert bv.read_version(path) is None


@pytest.fixture
def root_tmp():
    """Temp dir located INSIDE the project root.

    write_version refuses to touch paths outside PROJECT_ROOT (a security guard
    added on this branch), so fixtures that exercise the real writer must stay
    within the project tree.
    """
    d = bv.PROJECT_ROOT / ".bump_version_test_tmp"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_write_version_toml(root_tmp):
    path = root_tmp / "pyproject.toml"
    path.write_text('[project]\nname = "modulo"\nversion = "1.2.3"\n')
    bv.write_version(path, "1.2.3", "2.0.0")
    content = path.read_text()
    assert 'version = "2.0.0"' in content
    assert "1.2.3" not in content


def test_write_version_json(root_tmp):
    path = root_tmp / "package.json"
    path.write_text('{"name": "modulo", "version": "1.2.3"}\n')
    bv.write_version(path, "1.2.3", "1.3.0")
    content = path.read_text()
    assert '"version": "1.3.0"' in content
    assert "1.2.3" not in content


def test_write_version_refuses_outside_root(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "modulo"\nversion = "1.2.3"\n')
    with pytest.raises(ValueError, match="refusing to write version outside project root"):
        bv.write_version(path, "1.2.3", "2.0.0")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _setup_main(root_tmp):
    pyproject = root_tmp / "pyproject.toml"
    pyproject.write_text('[project]\nname = "modulo"\nversion = "1.2.3"\n')
    package = root_tmp / "package.json"
    package.write_text('{"name": "modulo", "version": "1.2.3"}\n')
    return pyproject, package


def test_main_bumps_patch_by_default(monkeypatch, root_tmp, capsys):
    pyproject, package = _setup_main(root_tmp)
    monkeypatch.setattr(sys, "argv", ["bump-version.py"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
    ):
        bv.main()
    assert 'version = "1.2.4"' in pyproject.read_text()
    assert '"version": "1.2.4"' in package.read_text()
    out = capsys.readouterr().out
    assert "Bumping patch version: 1.2.3 -> 1.2.4" in out


def test_main_bumps_requested_part(monkeypatch, root_tmp):
    pyproject, package = _setup_main(root_tmp)
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "minor"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
    ):
        bv.main()
    assert 'version = "1.3.0"' in pyproject.read_text()
    assert '"version": "1.3.0"' in package.read_text()


def test_main_bumps_major(monkeypatch, root_tmp):
    pyproject, package = _setup_main(root_tmp)
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "major"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
    ):
        bv.main()
    assert 'version = "2.0.0"' in pyproject.read_text()
    assert '"version": "2.0.0"' in package.read_text()


def test_main_exits_when_version_unreadable(monkeypatch, tmp_path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "modulo"\n')
    monkeypatch.setattr(sys, "argv", ["bump-version.py"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", tmp_path / "package.json"),
        pytest.raises(SystemExit) as exc,
    ):
        bv.main()
    assert exc.value.code == 1
    assert "could not read version from backend/pyproject.toml" in capsys.readouterr().out
