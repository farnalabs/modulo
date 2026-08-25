#!/usr/bin/env python3
"""Bump the project version across all relevant files."""  # noqa: N999

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PYPROJECT = PROJECT_ROOT / "backend" / "pyproject.toml"
FRONTEND_PACKAGE = PROJECT_ROOT / "frontend" / "package.json"


def read_version(path: Path) -> str | None:
    """Extract version from pyproject.toml or package.json."""
    content = path.read_text()
    if path.suffix == ".toml":
        m = re.search(r'version\s*=\s*"([^"]+)"', content)
    else:
        m = re.search(r'"version":\s*"([^"]+)"', content)
    return m.group(1) if m else None


def bump(version: str, part: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown part: {part}")


def write_version(path: Path, old_version: str, new_version: str):
    # Defense in depth: only ever write version files that live inside the project
    # root, so user-controlled inputs can never redirect the write elsewhere.
    resolved = path.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError(f"refusing to write version outside project root: {path}")
    content = path.read_text()
    if path.suffix == ".toml":
        content = content.replace(f'version = "{old_version}"', f'version = "{new_version}"')
    else:
        content = content.replace(f'"version": "{old_version}"', f'"version": "{new_version}"')
    path.write_text(content)
    print(f"  {path.name}: {old_version} -> {new_version}")


_VALID_PARTS = {"major", "minor", "patch"}


def main():
    part = sys.argv[1] if len(sys.argv) > 1 else "patch"
    if part not in _VALID_PARTS:
        print(f"ERROR: invalid part {part!r} (expected one of: {', '.join(sorted(_VALID_PARTS))})")
        sys.exit(1)
    old = read_version(BACKEND_PYPROJECT)
    if old is None:
        print("ERROR: could not read version from backend/pyproject.toml")
        sys.exit(1)
    new = bump(old, part)
    print(f"Bumping {part} version: {old} -> {new}")
    write_version(BACKEND_PYPROJECT, old, new)
    write_version(FRONTEND_PACKAGE, old, new)


if __name__ == "__main__":
    main()
