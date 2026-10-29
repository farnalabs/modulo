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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    return parser


def _resolve_beneath(root: Path, reference: str, label: str) -> Path:
    if not reference or ntpath.isabs(reference) or PureWindowsPath(reference).is_absolute():
        raise ValueError(f"{label} must be a relative path beneath the bundle root: {reference!r}")
    candidate = (root / Path(reference)).resolve()
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
    if ntpath.isabs(binary) or PureWindowsPath(binary).is_absolute():
        raise ValueError(f"{requirement_name} binary name must be relative: {binary!r}")


def validate(manifest_path: Path, schema_path: Path, bundle_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    root = bundle_path.resolve()
    _resolve_beneath(root, manifest["launcher"]["path"], "launcher path")
    _resolve_beneath(root, manifest["frontend"]["dist_path"], "frontend dist path")
    for name, requirement in manifest.get("native_requirements", {}).items():
        native_root = _resolve_beneath(root, requirement["relative_path"], f"{name} relative path")
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
    for name, requirement in manifest["native_requirements"].items():
        native_root = _resolve_beneath(root, requirement["relative_path"], f"{name} relative path")
        for binary in requirement["binaries"]:
            _validate_binary_name(binary, name)
            path = _resolve_beneath(native_root, binary, f"{name} binary")
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"required {name} binary is missing or empty: {path}")
            metadata = requirement.get("metadata", {}).get(binary)
            if metadata is not None and metadata.get("sha256") != _hash_file(path):
                raise ValueError(f"required {name} binary hash does not match manifest: {path}")
    manifest.setdefault("reproducibility", {})["validated_at_utc"] = datetime.now(UTC).isoformat()
    return manifest


def main() -> int:
    args = _parser().parse_args()
    validate(args.manifest, args.schema, args.bundle)
    print(f"Validated Windows bundle manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
