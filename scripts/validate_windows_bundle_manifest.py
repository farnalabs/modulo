"""Validate the Windows native bundle manifest and its referenced files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    return parser


def validate(manifest_path: Path, schema_path: Path, bundle_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    errors = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ValueError(f"manifest does not match schema: {details}")

    launcher = bundle_path / manifest["launcher"]["path"]
    if not launcher.is_file() or launcher.stat().st_size == 0:
        raise ValueError(f"manifest launcher is missing or empty: {launcher}")
    frontend = bundle_path / manifest["frontend"]["dist_path"]
    if not frontend.is_dir() or not any(frontend.iterdir()):
        raise ValueError(f"manifest frontend dist is missing or empty: {frontend}")
    for name, requirement in manifest["native_requirements"].items():
        native_root = bundle_path / requirement["relative_path"]
        for binary in requirement["binaries"]:
            path = native_root / binary
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"required {name} binary is missing or empty: {path}")
    return manifest


def main() -> int:
    args = _parser().parse_args()
    validate(args.manifest, args.schema, args.bundle)
    print(f"Validated Windows bundle manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
