"""Validate the native Windows bundle layout and signed release-manifest handoff.

Windows uses the same release manifest as Linux and macOS.  There is no second
Windows-only runtime manifest: ``RELEASE_MANIFEST.json`` and its ``.sig`` file
are produced by the release signing step and are consumed by the installer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Any

REQUIRED_DIRECTORIES = ("runtime", "pg", "redis", "bin", "frontend-dist", "backend/.venv")
REQUIRED_FILES = ("launcher",)
MANIFEST_NAME = "RELEASE_MANIFEST.json"
SIGNATURE_NAME = "RELEASE_MANIFEST.json.sig"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--check-bundle", action="store_true")
    return parser


def _relative(root: Path, reference: str, label: str) -> Path:
    if not reference or "\\" in reference:
        raise ValueError(f"{label} must use a non-empty forward-slash relative path")
    win = PureWindowsPath(reference)
    if win.is_absolute() or win.drive or reference.startswith("/"):
        raise ValueError(f"{label} must be relative to the bundle root: {reference!r}")
    candidate = (root / reference).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the bundle root: {reference!r}") from exc
    return candidate


def validate_layout(bundle_path: Path) -> None:
    root = bundle_path.resolve()
    if not root.is_dir():
        raise ValueError(f"bundle root is not a directory: {root}")
    missing_dirs = [name for name in REQUIRED_DIRECTORIES if not _relative(root, name, "bundle path").is_dir()]
    missing_files = [name for name in REQUIRED_FILES if not _relative(root, name, "bundle path").is_file()]
    if missing_dirs or missing_files:
        missing = ", ".join(missing_dirs + missing_files)
        raise ValueError(f"native bundle layout is incomplete; missing: {missing}")
    launcher = root / "launcher"
    if launcher.stat().st_size == 0:
        raise ValueError(f"native bundle launcher is empty: {launcher}")


def validate_manifest(manifest_path: Path, bundle_path: Path) -> dict[str, Any]:
    root = bundle_path.resolve()
    manifest = manifest_path.resolve()
    if manifest.name != MANIFEST_NAME:
        raise ValueError(f"Windows bundles use {MANIFEST_NAME}; found {manifest.name}")
    try:
        manifest.relative_to(root.parent)
    except ValueError as exc:
        raise ValueError(f"release manifest must be adjacent to the bundle root: {manifest}") from exc
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read release manifest: {manifest}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("release manifest must be a JSON object")
    for field in ("manifest_version", "release", "platform", "components", "artifacts"):
        if field not in payload:
            raise ValueError(f"release manifest is missing field: {field}")
    if payload["platform"] != "windows-amd64":
        raise ValueError("Windows release manifest platform must be 'windows-amd64'")
    signature = manifest.with_name(SIGNATURE_NAME)
    if not signature.is_file() or signature.stat().st_size == 0:
        raise ValueError(f"signed release manifest is missing: {signature}")
    return payload


def validate(bundle_path: Path, manifest_path: Path | None = None) -> dict[str, Any] | None:
    validate_layout(bundle_path)
    if manifest_path is None:
        return None
    return validate_manifest(manifest_path, bundle_path)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = validate(args.bundle, args.manifest)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if manifest is not None:
        print(f"Validated native Windows bundle and signed manifest: {args.bundle}")
    else:
        print(f"Validated native Windows bundle layout: {args.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
