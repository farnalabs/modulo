"""Contract tests for the provisioned native Windows bundle."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]
_VALIDATOR = _ROOT / "scripts" / "validate_windows_bundle_manifest.py"
_SCHEMA = _ROOT / "scripts" / "windows-bundle-manifest.schema.json"
_BUILDER = _ROOT / "scripts" / "build-windows-bundle.ps1"
_WORKFLOW = _ROOT / ".github/workflows/windows-installer.yml"
_PWSH = shutil.which("pwsh")
_SPEC = importlib.util.spec_from_file_location("windows_bundle_manifest", _VALIDATOR)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    bundle = tmp_path / "bundle"
    for relative in ("runtime", "pg", "redis", "bin", "frontend-dist", "backend/.venv"):
        (bundle / relative).mkdir(parents=True)
        (bundle / relative / "content").write_bytes(b"provisioned")
    (bundle / "launcher").write_bytes(b"native launcher")
    (bundle / "frontend-dist" / "index.html").write_text("<html />", encoding="utf-8")
    manifest = {
        "manifest_version": 1,
        "release": "bundle-v0.3.323",
        "platform": "windows-amd64",
        "generated_at": "test",
        "components": {"launcher": "native", "python": "3.12"},
        "artifacts": [
            {"name": "launcher", "sha256": hashlib.sha256(b"native launcher").hexdigest(), "version": "0.3.323"}
        ],
    }
    manifest_path = bundle / "RELEASE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    signature_path = bundle / "RELEASE_MANIFEST.json.sig"
    signature_path.write_text(json.dumps({"key_id": "missing", "signature": "0" * 128}), encoding="utf-8")
    return bundle, manifest_path, signature_path


def test_required_layout_is_explicit() -> None:
    assert set(_MODULE.REQUIRED_LAYOUT) == {
        "launcher",
        "runtime",
        "pg",
        "redis",
        "bin",
        "frontend-dist",
        "backend/.venv",
        "RELEASE_MANIFEST.json",
        "RELEASE_MANIFEST.json.sig",
    }


def test_validator_rejects_missing_layout(tmp_path: Path) -> None:
    bundle, manifest, signature = _bundle(tmp_path)
    shutil.rmtree(bundle / "redis")
    with pytest.raises(ValueError, match="redis"):
        _MODULE.validate(bundle, _SCHEMA, manifest, signature)


def test_validator_requires_signed_manifest_and_signature(tmp_path: Path) -> None:
    bundle, manifest, signature = _bundle(tmp_path)
    manifest.unlink()
    with pytest.raises(ValueError, match=r"RELEASE_MANIFEST\.json"):
        _MODULE.validate(bundle, _SCHEMA, manifest, signature)


def test_validator_fails_closed_on_invalid_signature(tmp_path: Path) -> None:
    bundle, manifest, signature = _bundle(tmp_path)
    with pytest.raises(ValueError, match="verification failed"):
        _MODULE.validate(bundle, _SCHEMA, manifest, signature)


def test_retired_contracts_are_not_referenced() -> None:
    paths = [
        _BUILDER,
        _VALIDATOR,
        _SCHEMA,
        Path(__file__),
        _ROOT / "deploy/windows/BUNDLE-BUILD.md",
        _ROOT / "deploy/windows/modulo.iss",
        _ROOT / "deploy/windows/INSTALLER-INFO.txt",
        _ROOT / "scripts/test-windows-installer.ps1",
        _WORKFLOW,
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    retired_builder_name = "Py" + "Installer"
    retired_manifest_name = "modulo-" + "runtime.json"
    retired_launcher_name = "modulo." + "exe"
    assert retired_builder_name not in text
    assert retired_builder_name.lower() not in text
    assert retired_manifest_name not in text
    assert retired_launcher_name not in text


def test_workflow_preserves_release_artifact_contract() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "name: windows-installer-${{ inputs.version }}" in workflow
    assert "modulo-${{ inputs.version }}-windows-amd64.exe" in workflow
    assert "if: ${{ inputs.mode == 'dry-run' }}" in workflow
    assert "actions/upload-artifact" in workflow


@pytest.mark.skipif(_PWSH is None, reason="pwsh 7 is required")
def test_builder_validate_only_does_not_create_artifact() -> None:
    result = __import__("subprocess").run(
        [
            _PWSH,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(_BUILDER),
            "-Version",
            "0.3.323",
            "-ValidateOnly",
            "-OutputDirectory",
            "build/windows-bundle",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert not (_ROOT / "build/windows-bundle").exists()
