"""Tests for the Windows native bundle manifest contract (FAR-2231)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VALIDATOR_PATH = _REPO_ROOT / "scripts" / "validate_windows_bundle_manifest.py"
_VALIDATOR_SPEC = importlib.util.spec_from_file_location("windows_bundle_manifest_validator", _VALIDATOR_PATH)
assert _VALIDATOR_SPEC is not None
assert _VALIDATOR_SPEC.loader is not None
_VALIDATOR_MODULE = importlib.util.module_from_spec(_VALIDATOR_SPEC)
_VALIDATOR_SPEC.loader.exec_module(_VALIDATOR_MODULE)
validate = _VALIDATOR_MODULE.validate
_SCHEMA = _REPO_ROOT / "scripts" / "windows-bundle-manifest.schema.json"


def _bundle(tmp_path: Path, *, include_native: bool = True) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    (bundle / "frontend-dist").mkdir(parents=True)
    (bundle / "frontend-dist" / "index.html").write_text("<html />", encoding="utf-8")
    (bundle / "modulo.exe").write_bytes(b"real-bundle-input")
    for service, binaries in {
        "postgres": ("postgres.exe", "psql.exe", "pg_dump.exe", "pg_isready.exe"),
        "redis": ("redis-server.exe", "redis-cli.exe"),
    }.items():
        for binary in binaries:
            path = bundle / service / "bin" / binary
            if include_native:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"native-input")
    manifest = {
        "schema_version": 1,
        "version": "0.3.323",
        "architecture": "amd64",
        "launcher": {"path": "modulo.exe", "entry_point": "modulo.cli.main:cli", "start_command": "modulo start"},
        "frontend": {"dist_path": "frontend-dist"},
        "native_requirements": {
            "postgresql": {
                "required": True,
                "relative_path": "postgres/bin",
                "binaries": ["postgres.exe", "psql.exe", "pg_dump.exe", "pg_isready.exe"],
            },
            "redis": {
                "required": True,
                "relative_path": "redis/bin",
                "binaries": ["redis-server.exe", "redis-cli.exe"],
            },
        },
    }
    manifest_path = bundle / "modulo-runtime.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle, manifest_path


def test_valid_manifest_describes_real_bundle_inputs(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)

    manifest = validate(manifest_path, _SCHEMA, bundle)

    assert manifest["architecture"] == "amd64"
    assert manifest["launcher"]["entry_point"] == "modulo.cli.main:cli"
    assert manifest["frontend"]["dist_path"] == "frontend-dist"
    assert manifest["native_requirements"]["postgresql"]["required"] is True


def test_missing_native_runtime_input_fails_closed(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    (bundle / "redis" / "bin" / "redis-server.exe").unlink()

    with pytest.raises(ValueError, match="redis binary is missing"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_schema_rejects_wrong_architecture(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["architecture"] = "linux-amd64"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="amd64"):
        validate(manifest_path, _SCHEMA, bundle)
