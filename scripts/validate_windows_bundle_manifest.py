"""Validate the Windows native bundle manifest and its referenced files."""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from jsonschema import Draft202012Validator

MANDATORY_BINARIES: dict[str, tuple[str, ...]] = {
    "postgresql": ("initdb.exe", "postgres.exe", "pg_isready.exe", "psql.exe", "pg_dump.exe"),
    "redis": ("redis-server.exe", "redis-cli.exe"),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    return parser


def _resolve_beneath(root: Path, reference: str, label: str) -> Path:
    if not reference:
        raise ValueError(f"{label} must be a relative path beneath the bundle root: {reference!r}")
    win_path = PureWindowsPath(reference)
    if win_path.is_absolute() or win_path.drive or ntpath.isabs(reference):
        raise ValueError(f"{label} must be a relative path beneath the bundle root: {reference!r}")
    normalized = reference.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(f"{label} must be a relative path beneath the bundle root: {reference!r}")
    candidate = (root / Path(normalized)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the bundle root: {reference!r}") from exc
    return candidate


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_binary_name(binary: str, requirement_name: str) -> None:
    if not binary or binary in {".", ".."} or "/" in binary or "\\" in binary:
        raise ValueError(f"{requirement_name} binary name must not contain path separators: {binary!r}")
    win_path = PureWindowsPath(binary)
    if win_path.is_absolute() or win_path.drive or ntpath.isabs(binary):
        raise ValueError(f"{requirement_name} binary name must be relative: {binary!r}")


def validate(manifest_path: Path, schema_path: Path, bundle_path: Path) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    schema_path = Path(schema_path)
    bundle_path = Path(bundle_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    root = bundle_path.resolve()

    launcher_path = manifest.get("launcher", {}).get("path")
    if launcher_path:
        _resolve_beneath(root, launcher_path, "launcher path")
    frontend_dist_path = manifest.get("frontend", {}).get("dist_path")
    if frontend_dist_path:
        _resolve_beneath(root, frontend_dist_path, "frontend dist path")
    for name, requirement in manifest.get("native_requirements", {}).items():
        rel_path = requirement.get("relative_path")
        if rel_path:
            native_root = _resolve_beneath(root, rel_path, f"{name} relative path")
            for binary in requirement.get("binaries", []):
                _validate_binary_name(binary, name)
                _resolve_beneath(native_root, binary, f"{name} binary")

    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    errors = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ValueError(f"manifest does not match schema: {details}")

    launcher = _resolve_beneath(root, manifest["launcher"]["path"], "launcher path")
    if not launcher.is_file() or launcher.stat().st_size == 0:
        raise ValueError(f"manifest launcher is missing or empty: {launcher}")
    frontend = _resolve_beneath(root, manifest["frontend"]["dist_path"], "frontend dist path")
    if not frontend.is_dir() or not any(frontend.iterdir()):
        raise ValueError(f"manifest frontend dist is missing or empty: {frontend}")

    for service, required_binaries in MANDATORY_BINARIES.items():
        req = manifest.get("native_requirements", {}).get(service, {})
        binaries = req.get("binaries", [])
        missing = [b for b in required_binaries if b not in binaries]
        if missing:
            raise ValueError(f"missing required {service} binary from inventory: {', '.join(missing)}")

    for name, requirement in manifest["native_requirements"].items():
        native_root = _resolve_beneath(root, requirement["relative_path"], f"{name} relative path")
        metadata_map = requirement.get("metadata")
        if not isinstance(metadata_map, dict):
            raise ValueError(f"required {name} metadata is missing from manifest")
        for binary in requirement["binaries"]:
            _validate_binary_name(binary, name)
            path = _resolve_beneath(native_root, binary, f"{name} binary")
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"required {name} binary is missing or empty: {path}")
            if binary not in metadata_map:
                raise ValueError(f"required {name} binary metadata is missing from manifest: {binary}")
            metadata = metadata_map[binary]
            if not isinstance(metadata, dict):
                raise ValueError(f"required {name} binary metadata is invalid: {binary}")
            if metadata.get("sha256") != _hash_file(path):
                raise ValueError(f"required {name} binary hash does not match manifest: {path}")
            if metadata.get("size_bytes") != path.stat().st_size:
                raise ValueError(f"required {name} binary size does not match manifest: {path}")

    manifest.setdefault("reproducibility", {})["validated_at_utc"] = datetime.now(UTC).isoformat()
    return manifest


def main() -> int:
    args = _parser().parse_args()
    validate(args.manifest, args.schema, args.bundle)
    print(f"Validated Windows bundle manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
