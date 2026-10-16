#!/usr/bin/env python3
# ruff: noqa: N999
"""Bump the project version across every version-carrying file.

Source of truth: ``backend/pyproject.toml`` ``[project].version``. The script
reads the current version from there and writes it to every mirror:

* ``backend/pyproject.toml``        ``version = "X.Y.Z"``
* ``frontend/package.json``         ``"version": "X.Y.Z"``
* ``deploy/helm/modulo/Chart.yaml`` ``version: X.Y.Z`` AND ``appVersion: "X.Y.Z"``

Every version file carries a plain numeric ``X.Y.Z`` only. A pre-release
suffix (``-rc1``) lives in GIT TAGS ONLY (e.g. ``v0.4.0-rc1``) and is never
written into these files; the bump arithmetic is deliberately numeric-only.

Fail-loud contract: every target must ACTUALLY change. If a target's expected
version pattern has drifted, or a target is already at the requested version,
the run aborts with a non-zero exit and a clear message BEFORE any file is
written - no silent no-ops, no half-updated trees.

Usage:
    python scripts/bump-version.py [major|minor|patch]   # relative bump
    python scripts/bump-version.py --set X.Y.Z           # explicit version
"""

import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PYPROJECT = PROJECT_ROOT / "backend" / "pyproject.toml"
FRONTEND_PACKAGE = PROJECT_ROOT / "frontend" / "package.json"
HELM_CHART = PROJECT_ROOT / "deploy" / "helm" / "modulo" / "Chart.yaml"

_VALID_PARTS = {"major", "minor", "patch"}
_SET_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _extract_version(content: str, path: Path) -> str | None:
    """Pull the version value out of one target file's content."""
    if path.suffix == ".toml":
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', content)
    elif path.suffix in (".yaml", ".yml"):
        # Chart.yaml: the top-level `version:` line (never `appVersion:`).
        m = re.search(r'(?m)^version:\s*"?([^"\s]+)"?', content)
    else:
        m = re.search(r'"version":\s*"([^"]+)"', content)
    return m.group(1) if m else None


def read_version(path: Path) -> str | None:
    """Extract version from pyproject.toml, package.json or Chart.yaml."""
    return _extract_version(path.read_text(encoding="utf-8"), path)


def bump(version: str, part: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown part: {part}")


def _field_patterns(path: Path, old_version: str, new_version: str) -> list[tuple[re.Pattern[str], str]]:
    """(pattern, replacement) pairs that carry the version in one target file."""
    if path.suffix == ".toml":
        return [
            (re.compile(rf'(?m)^version\s*=\s*"{re.escape(old_version)}"'), f'version = "{new_version}"'),
        ]
    if path.suffix in (".yaml", ".yml"):
        # Chart.yaml carries the version twice (chart `version` + `appVersion`);
        # both must move together or the chart is left incoherent.
        return [
            (re.compile(rf"(?m)^version:\s*{re.escape(old_version)}(?=$|[\s#])"), f"version: {new_version}"),
            (re.compile(rf'(?m)^appVersion:\s*"{re.escape(old_version)}"(?=$|[\s#])'), f'appVersion: "{new_version}"'),
        ]
    # Unanchored on purpose: package.json keeps the root "version" key first,
    # and the pattern's leading quote + closing quote make it exact either way.
    return [
        (
            re.compile(rf'"version":\s*"{re.escape(old_version)}"'),
            f'"version": "{new_version}"',
        ),
    ]


def _check_within_project_root(path: Path) -> None:
    # Defense in depth: only ever write version files that live inside the project
    # root, so user-controlled inputs can never redirect the write elsewhere.
    resolved = path.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError(f"refusing to write version outside project root: {path}")


def prepare_version_content(path: Path, old_version: str, new_version: str) -> str:
    """Return the post-bump content of one target file, or raise ValueError.

    Raises when the expected version pattern is absent (the file has drifted)
    or when the substitution would leave the file unchanged (it is already at
    the requested version). Nothing is written here.
    """
    _check_within_project_root(path)
    # Read with an explicit UTF-8 encoding and preserved line endings:
    # backend/pyproject.toml contains non-ASCII characters, and the platform
    # default encoding would either fail the read or round-trip the file
    # through CRLF on Windows, churning every line of the version file.
    with path.open(encoding="utf-8", newline="") as fh:
        content = fh.read()
    problems: list[str] = []
    updated = content
    for pattern, replacement in _field_patterns(path, old_version, new_version):
        updated, count = pattern.subn(replacement, updated, count=1)
        if count == 0:
            found = _extract_version(content, path)
            problems.append(
                f"{path.relative_to(PROJECT_ROOT)}: expected version {old_version} not found "
                f"(file carries {found!r}) - it has drifted from backend/pyproject.toml"
            )
    if problems:
        raise ValueError(" | ".join(problems))
    if updated == content:
        raise ValueError(f"{path.relative_to(PROJECT_ROOT)}: already at version {old_version} - file did not change")
    return updated


def _write_and_verify(path: Path, new_content: str, old_version: str, new_version: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new_content)
    # Verify the artefact, not the write call: read the file back.
    with path.open(encoding="utf-8", newline="") as fh:
        written = fh.read()
    found = _extract_version(written, path)
    if found != new_version:
        raise ValueError(
            f"{path.relative_to(PROJECT_ROOT)}: post-write verification failed - "
            f"file carries {found!r}, expected {new_version}"
        )
    print(f"  {path.name}: {old_version} -> {new_version}")


def write_version(path: Path, old_version: str, new_version: str) -> None:
    """Prepare, write and verify ONE target file - raises on any failure."""
    new_content = prepare_version_content(path, old_version, new_version)
    _write_and_verify(path, new_content, old_version, new_version)


def apply_version(old_version: str, new_version: str, paths: Sequence[Path]) -> None:
    """Validate every target first, then write them all.

    Validation failures (drift / already-at-target) are collected across all
    targets and raised BEFORE anything is written, so a failed run never
    leaves the tree half-updated.
    """
    problems: list[str] = []
    prepared: list[tuple[Path, str]] = []
    for path in paths:
        try:
            prepared.append((path, prepare_version_content(path, old_version, new_version)))
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError("no files were written:\n  - " + "\n  - ".join(problems))
    for path, content in prepared:
        _write_and_verify(path, content, old_version, new_version)


def _parse_args(argv: list[str]) -> tuple[Literal["bump", "set"], str]:
    """Return ("bump", part) or ("set", X.Y.Z); exits 1 on invalid input."""
    if not argv:
        return "bump", "patch"
    first = argv[0]
    if first == "--set":
        if len(argv) != 2:
            print("ERROR: --set requires exactly one version, e.g. --set 1.2.3")
            sys.exit(1)
        version = argv[1]
        if not _SET_VERSION_RE.match(version):
            print(
                f"ERROR: invalid --set version {version!r} (expected numeric X.Y.Z; "
                "prerelease suffixes like -rc1 belong in git tags only)"
            )
            sys.exit(1)
        return "set", version
    if first not in _VALID_PARTS:
        print(f"ERROR: invalid part {first!r} (expected one of: {', '.join(sorted(_VALID_PARTS))})")
        sys.exit(1)
    return "bump", first


def main() -> None:
    mode, value = _parse_args(sys.argv[1:])
    old = read_version(BACKEND_PYPROJECT)
    if old is None:
        print("ERROR: could not read version from backend/pyproject.toml")
        sys.exit(1)
    if mode == "set":
        new = value
        print(f"Setting version: {old} -> {new}")
    else:
        new = bump(old, value)
        print(f"Bumping {value} version: {old} -> {new}")
    try:
        apply_version(old, new, (BACKEND_PYPROJECT, FRONTEND_PACKAGE, HELM_CHART))
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
