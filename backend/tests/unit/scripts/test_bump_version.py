"""Unit tests for bump-version.py — version sync across pyproject.toml, package.json and Chart.yaml."""

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


def test_read_version_chart_yaml(tmp_path):
    path = tmp_path / "Chart.yaml"
    path.write_text('apiVersion: v2\nname: modulo\nversion: 1.2.3\nappVersion: "1.2.3"\n')
    assert bv.read_version(path) == "1.2.3"


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


def test_write_version_chart_updates_both_fields(root_tmp):
    """Chart.yaml carries the version twice; both fields must move together."""
    path = root_tmp / "Chart.yaml"
    path.write_text('apiVersion: v2\nname: modulo\nversion: 1.2.3\nappVersion: "1.2.3"\n')
    bv.write_version(path, "1.2.3", "2.0.0")
    content = path.read_text()
    assert "version: 2.0.0" in content
    assert 'appVersion: "2.0.0"' in content
    assert "1.2.3" not in content


def test_write_version_drifted_target_raises(root_tmp):
    """A file whose expected version pattern is absent must raise, not no-op.

    The pre-FAR-1865 writer ran an unasserted str.replace: with package.json
    at 9.9.9 the replace found nothing, the file was still "written", and the
    run printed the version change as if it had happened.
    """
    path = root_tmp / "package.json"
    path.write_text('{"name": "modulo", "version": "9.9.9"}\n')
    with pytest.raises(ValueError, match="drifted"):
        bv.write_version(path, "1.2.3", "2.0.0")
    # prepare raises before any write, so the drifted file is untouched
    assert '"version": "9.9.9"' in path.read_text()


def test_write_version_chart_partial_drift_raises(root_tmp):
    """A chart whose appVersion drifted must fail before anything is written."""
    path = root_tmp / "Chart.yaml"
    path.write_text('apiVersion: v2\nname: modulo\nversion: 1.2.3\nappVersion: "9.9.9"\n')
    with pytest.raises(ValueError, match="drifted"):
        bv.write_version(path, "1.2.3", "2.0.0")
    assert 'appVersion: "9.9.9"' in path.read_text()


def test_write_version_already_at_target_raises(root_tmp):
    """Setting a file to the version it already has is a no-op -> hard error."""
    path = root_tmp / "pyproject.toml"
    path.write_text('[project]\nname = "modulo"\nversion = "1.2.3"\n')
    before = path.read_bytes()
    with pytest.raises(ValueError, match=r"already at version 1\.2\.3"):
        bv.write_version(path, "1.2.3", "1.2.3")
    assert path.read_bytes() == before


def test_write_version_preserves_crlf(root_tmp):
    """A CRLF version file must round-trip byte-exactly.

    Without ``newline=""`` the read normalizes CRLF to LF and the write emits
    native newlines, silently rewriting every line of the file. This is the
    regression the explicit ``newline=""`` guards against.
    """
    path = root_tmp / "pyproject.toml"
    path.write_bytes(b'[project]\r\nname = "modulo"\r\nversion = "1.2.3"\r\n')
    bv.write_version(path, "1.2.3", "2.0.0")
    data = path.read_bytes()
    assert b'version = "2.0.0"\r\n' in data
    assert data.count(b"\r\n") == 3
    assert data.replace(b"\r\n", b"").count(b"\n") == 0


def test_write_version_preserves_utf8_non_ascii(root_tmp):
    """Non-ASCII bytes (em dashes) must survive a version write byte-exactly."""
    path = root_tmp / "pyproject.toml"
    original = '[project]\nname = "modulo"\nversion = "1.2.3"\n# \u2014 em dash\n'.encode()
    path.write_bytes(original)
    bv.write_version(path, "1.2.3", "2.0.0")
    data = path.read_bytes()
    assert b'version = "2.0.0"' in data
    assert "\u2014".encode() in data
    assert data == original.replace(b'"1.2.3"', b'"2.0.0"')


def test_write_version_refuses_outside_root(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "modulo"\nversion = "1.2.3"\n')
    with pytest.raises(ValueError, match="refusing to write version outside project root"):
        bv.write_version(path, "1.2.3", "2.0.0")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _setup_main(root_tmp):
    """Create all three version targets at 1.2.3 and return them."""
    pyproject = root_tmp / "pyproject.toml"
    pyproject.write_text('[project]\nname = "modulo"\nversion = "1.2.3"\n')
    package = root_tmp / "package.json"
    package.write_text('{"name": "modulo", "version": "1.2.3"}\n')
    chart = root_tmp / "Chart.yaml"
    chart.write_text('apiVersion: v2\nname: modulo\nversion: 1.2.3\nappVersion: "1.2.3"\n')
    return pyproject, package, chart


def test_main_bumps_patch_by_default(monkeypatch, root_tmp, capsys):
    pyproject, package, chart = _setup_main(root_tmp)
    monkeypatch.setattr(sys, "argv", ["bump-version.py"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
        patch.object(bv, "HELM_CHART", chart),
    ):
        bv.main()
    assert 'version = "1.2.4"' in pyproject.read_text()
    assert '"version": "1.2.4"' in package.read_text()
    assert 'appVersion: "1.2.4"' in chart.read_text()
    out = capsys.readouterr().out
    assert "Bumping patch version: 1.2.3 -> 1.2.4" in out


def test_main_bumps_requested_part(monkeypatch, root_tmp):
    pyproject, package, chart = _setup_main(root_tmp)
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "minor"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
        patch.object(bv, "HELM_CHART", chart),
    ):
        bv.main()
    assert 'version = "1.3.0"' in pyproject.read_text()
    assert '"version": "1.3.0"' in package.read_text()
    assert "version: 1.3.0" in chart.read_text()


def test_main_bumps_major(monkeypatch, root_tmp):
    pyproject, package, chart = _setup_main(root_tmp)
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "major"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
        patch.object(bv, "HELM_CHART", chart),
    ):
        bv.main()
    assert 'version = "2.0.0"' in pyproject.read_text()
    assert '"version": "2.0.0"' in package.read_text()
    assert "version: 2.0.0" in chart.read_text()


def test_main_set_writes_explicit_version(monkeypatch, root_tmp, capsys):
    """--set X.Y.Z writes the exact version to all three targets."""
    pyproject, package, chart = _setup_main(root_tmp)
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "--set", "2.5.0"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
        patch.object(bv, "HELM_CHART", chart),
    ):
        bv.main()
    assert 'version = "2.5.0"' in pyproject.read_text()
    assert '"version": "2.5.0"' in package.read_text()
    chart_content = chart.read_text()
    assert "version: 2.5.0" in chart_content
    assert 'appVersion: "2.5.0"' in chart_content
    out = capsys.readouterr().out
    assert "Setting version: 1.2.3 -> 2.5.0" in out


def test_main_set_rejects_prerelease_suffix(monkeypatch, capsys):
    """Numeric-only in files: -rc1 suffixes belong to git tags, never manifests."""
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "--set", "1.2.3-rc1"])
    with pytest.raises(SystemExit) as exc:
        bv.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "invalid --set version" in out
    assert "git tags only" in out


def test_main_fails_loud_when_a_target_has_drifted(monkeypatch, root_tmp, capsys):
    """FAIL-LOUD PROOF (FAR-1865): a drifted mirror aborts the whole run.

    The pre-FAR-1865 writer never asserted that its replace landed: with
    package.json at 9.9.9 it wrote pyproject at the new version, left
    package.json untouched, printed both lines as if they had changed, and
    exited 0. Against that old behaviour this test FAILS (main() returns
    normally instead of raising SystemExit, and pyproject gets bumped).
    """
    pyproject, package, chart = _setup_main(root_tmp)
    package.write_text('{"name": "modulo", "version": "9.9.9"}\n')
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "patch"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
        patch.object(bv, "HELM_CHART", chart),
        pytest.raises(SystemExit) as exc,
    ):
        bv.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "no files were written" in out
    assert "package.json" in out
    assert "drifted from backend/pyproject.toml" in out
    # Validation runs before any write: the valid targets stayed untouched.
    assert 'version = "1.2.3"' in pyproject.read_text()
    assert "version: 1.2.3" in chart.read_text()


def test_main_set_fails_when_all_targets_already_at_version(monkeypatch, root_tmp, capsys):
    """FAIL-LOUD PROOF (FAR-1865): --set to the current value is a hard error.

    Against the old behaviour this test FAILS too: --set was not a recognised
    mode, so the run aborted on "invalid part" and never emitted the
    already-at-target diagnosis asserted below.
    """
    pyproject, package, chart = _setup_main(root_tmp)
    before = (pyproject.read_bytes(), package.read_bytes(), chart.read_bytes())
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "--set", "1.2.3"])
    with (
        patch.object(bv, "BACKEND_PYPROJECT", pyproject),
        patch.object(bv, "FRONTEND_PACKAGE", package),
        patch.object(bv, "HELM_CHART", chart),
        pytest.raises(SystemExit) as exc,
    ):
        bv.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "no files were written" in out
    assert "already at version 1.2.3" in out
    after = (pyproject.read_bytes(), package.read_bytes(), chart.read_bytes())
    assert after == before


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


def test_main_invalid_part_exits(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bump-version.py", "nonsense"])
    with pytest.raises(SystemExit) as exc:
        bv.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR: invalid part 'nonsense'" in out
    assert "expected one of" in out
