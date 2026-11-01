"""Contract tests for the fail-closed native Windows bundle boundary."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VALIDATOR_PATH = _REPO_ROOT / "scripts" / "validate_windows_bundle_manifest.py"
_BUILDER = _REPO_ROOT / "scripts" / "build-windows-bundle.ps1"
_SPEC = importlib.util.spec_from_file_location("windows_bundle_validator", _VALIDATOR_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
validate_layout = _MODULE.validate_layout
validate_manifest = _MODULE.validate_manifest
_PWSH = shutil.which("pwsh")


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    for directory in ("runtime", "pg", "redis", "bin", "frontend-dist", "backend/.venv"):
        (root / directory).mkdir(parents=True)
    (root / "frontend-dist/index.html").write_text("<html />", encoding="utf-8")
    (root / "launcher").write_text("native launcher", encoding="utf-8")
    return root


def test_layout_matches_native_bundle_contract(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    validate_layout(root)
    assert (root / "runtime").is_dir()


def test_layout_rejects_missing_runtime_directory(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "runtime").rmdir()
    with pytest.raises(ValueError, match="runtime"):
        validate_layout(root)


def test_layout_rejects_missing_launcher(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "launcher").unlink()
    with pytest.raises(ValueError, match="launcher"):
        validate_layout(root)


def test_manifest_handoff_requires_signed_release_manifest(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    manifest = root.parent / "RELEASE_MANIFEST.json"
    manifest.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "release": "bundle-v0.4.0",
                "platform": "windows-amd64",
                "components": {"python": "3.12"},
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="signed release manifest is missing"):
        validate_manifest(manifest, root)
    (root.parent / "RELEASE_MANIFEST.json.sig").write_text('{"key_id":"test"}', encoding="utf-8")
    assert validate_manifest(manifest, root)["platform"] == "windows-amd64"


def test_manifest_rejects_windows_only_manifest_contract(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    manifest = root.parent / "modulo-runtime.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape("RELEASE_MANIFEST.json")):
        validate_manifest(manifest, root)


def test_builder_contains_no_obsolete_manifest_or_pyinstaller_contract() -> None:
    source = _BUILDER.read_text(encoding="utf-8").lower()
    assert "pyinstaller" not in source
    assert "modulo-runtime.json" not in source
    assert "postgresdirectory" not in source
    assert "redisdirectory" not in source


@pytest.mark.skipif(_PWSH is None, reason="pwsh 7 is required to exercise the builder")
def test_builder_validate_only_fails_closed_without_windows_pins(tmp_path: Path) -> None:
    fake = tmp_path / "repo"
    (fake / "scripts").mkdir(parents=True)
    (fake / "frontend").mkdir()
    shutil.copy2(_BUILDER, fake / "scripts/build-windows-bundle.ps1")
    shutil.copy2(_REPO_ROOT / "scripts/bundle-versions.env", fake / "scripts/bundle-versions.env")
    (fake / "frontend/package.json").write_text('{"version":"0.4.0"}', encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [
            _PWSH,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(fake / "scripts/build-windows-bundle.ps1"),
            "-ValidateOnly",
        ],
        cwd=fake,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode != 0
    assert "CPython Windows pin" in result.stdout
    assert "Redis Windows source" in result.stdout
    assert "not provisioned" in result.stdout
