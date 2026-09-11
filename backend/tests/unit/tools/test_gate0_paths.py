"""Gate 0 path-filter coverage assertion (FAR-673, ADR 031 Decision 10).

The Gate 0 workflow's pull_request trigger is path-filtered, and the
filter list is committed to the repo (``.github/native-gate0-paths.txt``)
so it evolves with the code it protects instead of hiding inside a
workflow YAML expression. This test is the safety net that keeps the
checked-in filter honest: the modules the native launcher boots directly
must remain covered by an always-run pattern, or a launcher-critical
regression could pass Compose-only CI and only surface as a failed
release smoke AFTER a tag is cut.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents if (parent / ".github").is_dir()),
    None,
)
_PATHS_FILE = _REPO_ROOT / ".github" / "native-gate0-paths.txt" if _REPO_ROOT is not None else None


def _load_patterns() -> list[str]:
    assert _PATHS_FILE is not None, "could not locate the repo root from the test file"
    lines = [line.split("#", 1)[0].strip() for line in _PATHS_FILE.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def _fnmatch_posix(path: str, pattern: str) -> bool:
    # GitHub's paths filter is a glob over the repo-relative POSIX path with
    # 'match any part' semantics; for the patterns we assert (dir prefixes
    # with ** and exact-file patterns) a straightforward translation is a
    # faithful check of the two shapes we actually commit.
    if pattern.endswith("/**"):
        prefix = pattern[: -len("/**")]
        return path == prefix or path.startswith(prefix + "/")
    return path == pattern


def _covered(patterns: list[str], path: str) -> bool:
    return any(_fnmatch_posix(path, pattern) for pattern in patterns)


def test_gate0_paths_file_exists_and_is_nonempty() -> None:
    assert _PATHS_FILE is not None and _PATHS_FILE.is_file(), (
        f"{_PATHS_FILE} must exist: the Gate 0 workflow trigger filter is checked in, not inline"
    )
    assert _load_patterns(), "the Gate 0 paths filter must contain at least one always-run pattern"


def test_gate0_covers_bundled_boot_critical_modules() -> None:
    patterns = _load_patterns()
    critical = [
        "backend/src/modulo/db/url_utils.py",
        "backend/src/modulo/settings.py",
        "backend/src/modulo/api/main.py",
    ]
    uncovered = [path for path in critical if not _covered(patterns, path)]
    assert not uncovered, (
        "Gate 0 always-run filter no longer covers critical launcher-boot modules: "
        f"{uncovered}. Restore the pattern in {_PATHS_FILE} or update this test with "
        "an explicit, reviewed justification."
    )


def test_gate0_covers_bundled_boot_critical_directories() -> None:
    patterns = _load_patterns()
    critical_dirs = [
        "backend/src/modulo/db/migrations",
        "backend/src/modulo/db/settings_resolver.py",
        "deploy/fly/entrypoint.sh",
    ]
    uncovered = [path for path in critical_dirs if not _covered(patterns, path)]
    assert not uncovered, f"Gate 0 always-run filter lost directory coverage for: {uncovered}"
