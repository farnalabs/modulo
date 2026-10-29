"""Contract-validation tests for the Windows native bundle manifest (FAR-2231)."""

from __future__ import annotations

import hashlib
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


_POSTGRES_BINARIES = ("initdb.exe", "postgres.exe", "pg_isready.exe", "psql.exe", "pg_dump.exe")
_REDIS_BINARIES = ("redis-server.exe", "redis-cli.exe")


def _bundle(tmp_path: Path, *, include_native: bool = True) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    (bundle / "frontend-dist").mkdir(parents=True)
    (bundle / "frontend-dist" / "index.html").write_text("<html />", encoding="utf-8")
    (bundle / "modulo.exe").write_bytes(b"contract-only-launcher")
    metadata: dict[str, dict[str, dict[str, int | str]]] = {"postgresql": {}, "redis": {}}
    for service, binaries in {"postgresql": _POSTGRES_BINARIES, "redis": _REDIS_BINARIES}.items():
        for binary in binaries:
            path = bundle / ("postgres" if service == "postgresql" else "redis") / "bin" / binary
            if include_native:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"temporary contract input: {binary}".encode())
                metadata[service][binary] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                }
    manifest = {
        "schema_version": 1,
        "version": "0.3.323",
        "architecture": "amd64",
        "runtime_support": {"status": "unsupported", "reason": "Windows launcher support is pending FAR-2229."},
        "launcher": {"path": "modulo.exe", "entry_point": "modulo.cli.main:cli", "start_command": "modulo start"},
        "frontend": {"dist_path": "frontend-dist"},
        "native_requirements": {
            "postgresql": {
                "required": True,
                "relative_path": "postgres/bin",
                "binaries": list(_POSTGRES_BINARIES),
                "metadata": metadata["postgresql"],
            },
            "redis": {
                "required": True,
                "relative_path": "redis/bin",
                "binaries": list(_REDIS_BINARIES),
                "metadata": metadata["redis"],
            },
        },
        "reproducibility": {
            "tool_versions": {"pnpm": "11.28.8", "python": "3.12", "pyinstaller": "6.0"},
            "validated_at_utc": "2026-01-01T00:00:00+00:00",
        },
    }
    manifest_path = bundle / "modulo-runtime.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle, manifest_path


def test_valid_manifest_is_contract_validation_only(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)

    manifest = validate(manifest_path, _SCHEMA, bundle)

    assert manifest["runtime_support"]["status"] == "unsupported"
    assert set(manifest["native_requirements"]["postgresql"]["binaries"]) == set(_POSTGRES_BINARIES)


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


@pytest.mark.parametrize(
    "path",
    [
        "../modulo.exe",
        r"..\modulo.exe",
        r"subdir\..\..\escape.exe",
        r"C:\\outside\\modulo.exe",
        r"\outside\modulo.exe",
    ],
)
def test_manifest_rejects_rooted_or_traversing_launcher_path(tmp_path: Path, path: str) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["launcher"]["path"] = path
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="launcher path"):
        validate(manifest_path, _SCHEMA, bundle)


@pytest.mark.parametrize(
    "path",
    [
        r"..\outside",
        r"postgres\..\..\escape",
    ],
)
def test_manifest_rejects_backslash_path_traversal_on_relative_path(tmp_path: Path, path: str) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["postgresql"]["relative_path"] = path
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="relative path"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_missing_metadata_entry_for_listed_binary(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    del data["native_requirements"]["postgresql"]["metadata"]["initdb.exe"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="metadata is missing from manifest"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_missing_required_binary_from_inventory(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["postgresql"]["binaries"].remove("initdb.exe")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required postgresql binary from inventory"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_missing_required_redis_binary_from_inventory(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["redis"]["binaries"].remove("redis-server.exe")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required redis binary from inventory"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_binary_path_separator(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["native_requirements"]["redis"]["binaries"][0] = "..\\redis-server.exe"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="binary name"):
        validate(manifest_path, _SCHEMA, bundle)


def test_manifest_rejects_changed_native_hash(tmp_path: Path) -> None:
    bundle, manifest_path = _bundle(tmp_path)
    (bundle / "postgres" / "bin" / "initdb.exe").write_bytes(b"changed")

    with pytest.raises(ValueError, match="hash does not match"):
        validate(manifest_path, _SCHEMA, bundle)
