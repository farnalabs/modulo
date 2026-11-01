"""Validate the Windows native bundle manifest and its referenced files.

The module is also the single source of the two bundle contracts the PowerShell
builder consumes, so neither has a second hard-coded copy:

``--print-inventory``
    Print the required native binaries (and their bundle-relative location) per
    service as JSON.
``--check-bundle DIR``
    Refuse a bundle that still contains PyInstaller build scaffolding.

Manifest validation exits non-zero with a single ``error: ...`` line rather than
a traceback, and never mutates the manifest it validated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import sys
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Any, TypedDict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class NativeRequirement(TypedDict):
    """One native service the bundle must ship, and where it lives in the bundle."""

    relative_path: str
    binaries: tuple[str, ...]


#: The authoritative native-binary inventory. The builder reads this through
#: ``--print-inventory`` instead of keeping its own list.
NATIVE_INVENTORY: dict[str, NativeRequirement] = {
    "postgresql": {
        "relative_path": "postgres/bin",
        "binaries": ("initdb.exe", "postgres.exe", "pg_isready.exe", "psql.exe", "pg_dump.exe"),
    },
    "redis": {
        "relative_path": "redis/bin",
        "binaries": ("redis-server.exe", "redis-cli.exe"),
    },
}

#: PyInstaller scaffolding. It is built in a temp staging directory and must
#: never be copied into - or survive inside - the emitted bundle.
FORBIDDEN_BUNDLE_ENTRIES: tuple[str, ...] = (
    "pyinstaller",
    "pyinstaller-work",
    "pyinstaller-spec",
    "pyinstaller-entrypoint.py",
)

_SCHEMA_FILENAME = "windows-bundle-manifest.schema.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, help="Manifest file to validate.")
    parser.add_argument("--schema", type=Path, help="JSON Schema the manifest must satisfy.")
    parser.add_argument("--bundle", type=Path, help="Bundle root the manifest describes.")
    parser.add_argument(
        "--print-inventory", action="store_true", help="Print the native binary inventory as JSON and exit."
    )
    parser.add_argument(
        "--check-bundle", type=Path, default=None, help="Check a bundle for build scaffolding and exit."
    )
    return parser


def inventory_document() -> dict[str, dict[str, Any]]:
    """The inventory in JSON-serialisable form (tuples become lists)."""
    return {
        name: {"relative_path": requirement["relative_path"], "binaries": list(requirement["binaries"])}
        for name, requirement in NATIVE_INVENTORY.items()
    }


def assert_no_scaffolding(bundle_root: Path) -> None:
    """Raise when the bundle contains a build artefact that must not ship."""
    root = Path(bundle_root)
    if not root.is_dir():
        raise ValueError(f"bundle root is not a directory: {root}")
    forbidden = {entry.casefold() for entry in FORBIDDEN_BUNDLE_ENTRIES}
    offenders = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.name.casefold() in forbidden)
    if offenders:
        raise ValueError(f"bundle contains build scaffolding that must not ship: {', '.join(offenders)}")


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


def _repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_local_input(path: Path, label: str, *, allowed_root: Path | None = None) -> Path:
    """Resolve a CLI input and require it to stay in the intended local boundary."""
    candidate = path.expanduser().resolve()
    root = (allowed_root or _repository_root()).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        boundary = "the repository root" if allowed_root is None else "the allowed local root"
        raise ValueError(f"{label} must be beneath {boundary}: {path}") from exc
    return candidate


def _resolve_schema_path(path: Path) -> Path:
    schema = _resolve_local_input(path, "schema path", allowed_root=_repository_root() / "scripts")
    if schema.name != _SCHEMA_FILENAME:
        raise ValueError(f"schema path must name {_SCHEMA_FILENAME}: {path}")
    return schema


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return value


def _validate_binary_name(binary: str, requirement_name: str) -> None:
    if not binary or binary in {".", ".."} or "/" in binary or "\\" in binary:
        raise ValueError(f"{requirement_name} binary name must not contain path separators: {binary!r}")
    win_path = PureWindowsPath(binary)
    if win_path.is_absolute() or win_path.drive or ntpath.isabs(binary):
        raise ValueError(f"{requirement_name} binary name must be relative: {binary!r}")


def _assert_known_layout(name: str, relative_path: str) -> None:
    expected = NATIVE_INVENTORY[name]["relative_path"]
    if relative_path != expected:
        raise ValueError(f"{name} relative_path must be {expected!r} (native binary inventory); got {relative_path!r}")


def _validate_manifest_paths(manifest: dict[str, Any], root: Path) -> None:
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
            if name in NATIVE_INVENTORY:
                _assert_known_layout(name, rel_path)
            for binary in requirement.get("binaries", []):
                _validate_binary_name(binary, name)
                _resolve_beneath(native_root, binary, f"{name} binary")


def _validate_manifest_schema(manifest: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ValueError(f"manifest does not match schema: {details}")


def _validate_required_files(manifest: dict[str, Any], root: Path) -> None:
    launcher = _resolve_beneath(root, manifest["launcher"]["path"], "launcher path")
    if not launcher.is_file() or launcher.stat().st_size == 0:
        raise ValueError(f"manifest launcher is missing or empty: {launcher}")
    frontend = _resolve_beneath(root, manifest["frontend"]["dist_path"], "frontend dist path")
    if not frontend.is_dir() or not any(frontend.iterdir()):
        raise ValueError(f"manifest frontend dist is missing or empty: {frontend}")


def _validate_inventory_presence(manifest: dict[str, Any]) -> None:
    requirements = manifest.get("native_requirements", {})
    for service, requirement in NATIVE_INVENTORY.items():
        binaries = requirements.get(service, {}).get("binaries", [])
        missing = [binary for binary in requirement["binaries"] if binary not in binaries]
        if missing:
            raise ValueError(f"missing required {service} binary from inventory: {', '.join(missing)}")


def _require_metadata_map(name: str, requirement: dict[str, Any]) -> dict[str, Any]:
    """The per-binary metadata map for one service, or a failure when it is absent."""
    metadata_map = requirement.get("metadata")
    if not isinstance(metadata_map, dict):
        raise ValueError(f"required {name} metadata is missing from manifest")
    return metadata_map


def _require_binary_file(name: str, path: Path) -> int:
    """Require a bundled binary to exist and be non-empty; return its size in bytes."""
    if not path.is_file():
        raise ValueError(f"required {name} binary is missing or empty: {path}")
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"required {name} binary is missing or empty: {path}")
    return size


def _assert_metadata_matches(name: str, binary: str, metadata: Any, path: Path, size: int) -> None:
    """Require the manifest's recorded sha256 and size to describe the bundled file itself."""
    if not isinstance(metadata, dict):
        raise ValueError(f"required {name} binary metadata is invalid: {binary}")
    if metadata.get("sha256") != _hash_file(path):
        raise ValueError(f"required {name} binary hash does not match manifest: {path}")
    if metadata.get("size_bytes") != size:
        raise ValueError(f"required {name} binary size does not match manifest: {path}")


def _validate_requirement_binaries(
    name: str, requirement: dict[str, Any], native_root: Path, metadata_map: dict[str, Any]
) -> None:
    """Verify every binary one service must ship exists on disk and matches its manifest entry."""
    for binary in requirement["binaries"]:
        _validate_binary_name(binary, name)
        path = _resolve_beneath(native_root, binary, f"{name} binary")
        size = _require_binary_file(name, path)
        if binary not in metadata_map:
            raise ValueError(f"required {name} binary metadata is missing from manifest: {binary}")
        _assert_metadata_matches(name, binary, metadata_map[binary], path, size)


def _validate_binary_metadata(manifest: dict[str, Any], root: Path) -> None:
    for name, requirement in manifest["native_requirements"].items():
        native_root = _resolve_beneath(root, requirement["relative_path"], f"{name} relative path")
        metadata_map = _require_metadata_map(name, requirement)
        _validate_requirement_binaries(name, requirement, native_root, metadata_map)


def validate(manifest_path: Path, schema_path: Path, bundle_path: Path) -> dict[str, Any]:
    """Validate a manifest against the schema, the inventory and the emitted bundle."""
    root = Path(bundle_path).resolve()
    manifest_file = _resolve_local_input(Path(manifest_path), "manifest path", allowed_root=root)
    schema_file = _resolve_schema_path(Path(schema_path))
    manifest = _load_json(manifest_file, "manifest")
    schema = _load_json(schema_file, "schema")
    assert_no_scaffolding(root)
    _validate_manifest_paths(manifest, root)
    _validate_manifest_schema(manifest, schema)
    _validate_required_files(manifest, root)
    _validate_inventory_presence(manifest)
    _validate_binary_metadata(manifest, root)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.print_inventory:
            print(json.dumps(inventory_document(), indent=2))
            return 0
        if args.check_bundle is not None:
            assert_no_scaffolding(args.check_bundle)
            print(f"Bundle hygiene OK: {args.check_bundle}")
            return 0
        if args.manifest is None or args.schema is None or args.bundle is None:
            parser.error("--manifest, --schema and --bundle are required (or use --print-inventory / --check-bundle)")
        validate(args.manifest, args.schema, args.bundle)
    except (ValueError, OSError, SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Validated Windows bundle manifest: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
