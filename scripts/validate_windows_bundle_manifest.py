"""Validate the provisioned native Windows bundle contract."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

REQUIRED_LAYOUT: dict[str, str] = {
    "launcher": "file",
    "runtime": "directory",
    "pg": "directory",
    "redis": "directory",
    "bin": "directory",
    "frontend-dist": "directory",
    "backend/.venv": "directory",
    "RELEASE_MANIFEST.json": "file",
    "RELEASE_MANIFEST.json.sig": "file",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--signed-manifest", type=Path)
    parser.add_argument("--signature", type=Path)
    parser.add_argument("--version")
    return parser


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _require_path(root: Path, relative: str, kind: str) -> Path:
    path = root / relative
    if kind == "file" and (not path.is_file() or path.stat().st_size == 0):
        raise ValueError(f"required bundle file is missing or empty: {relative}")
    if kind == "directory" and (not path.is_dir() or not any(path.iterdir())):
        raise ValueError(f"required bundle directory is missing or empty: {relative}")
    return path


def validate_layout(bundle: Path) -> None:
    root = bundle.resolve()
    if not root.is_dir():
        raise ValueError(f"bundle root is not a directory: {bundle}")
    for relative, kind in REQUIRED_LAYOUT.items():
        _require_path(root, relative, kind)


def validate_signed_manifest(
    manifest_path: Path,
    signature_path: Path,
    *,
    artifacts_root: Path,
    version: str | None = None,
) -> dict[str, Any]:
    if not manifest_path.is_file() or manifest_path.stat().st_size == 0:
        raise ValueError(f"signed release manifest is missing or empty: {manifest_path}")
    if not signature_path.is_file() or signature_path.stat().st_size == 0:
        raise ValueError(f"release manifest signature is missing or empty: {signature_path}")
    backend_src = Path(__file__).resolve().parents[1] / "backend" / "src"
    sys.path.insert(0, str(backend_src))
    try:
        from modulo.launcher import manifest as release_manifest

        parsed = release_manifest.verify_release_from_files(
            manifest_path=manifest_path,
            signature_path=signature_path,
            artifacts_root=artifacts_root,
            artifact_names=None,
        )
    except (OSError, ValueError, release_manifest.ManifestSecurityError) as exc:
        raise ValueError(f"signed release manifest verification failed: {exc}") from exc
    if version is not None and parsed.release != f"bundle-v{version}":
        raise ValueError(f"signed release manifest release does not match version {version}: {parsed.release}")
    return _load_json(manifest_path, "signed release manifest")


def validate_schema(manifest: dict[str, Any], schema_path: Path) -> None:
    schema = _load_json(schema_path, "schema")
    errors = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        raise ValueError("manifest does not match schema: " + "; ".join(error.message for error in errors))


def validate(
    bundle_path: Path, schema_path: Path, manifest_path: Path, signature_path: Path, *, version: str | None = None
) -> dict[str, Any]:
    bundle = bundle_path.resolve()
    validate_layout(bundle)
    manifest = validate_signed_manifest(
        manifest_path.resolve(),
        signature_path.resolve(),
        artifacts_root=bundle,
        version=version,
    )
    validate_schema(manifest, schema_path.resolve())
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.bundle is None or args.schema is None or args.signed_manifest is None or args.signature is None:
            raise ValueError("--bundle, --schema, --signed-manifest and --signature are required")
        validate(args.bundle, args.schema, args.signed_manifest, args.signature, version=args.version)
    except (ValueError, OSError, SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Validated signed Windows bundle contract: {args.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
