#!/usr/bin/env python3
"""Fast best-guess affected-tests gate for pre-push.

Runs the changed test files plus candidate test files for changed source
modules — NOT the full backend suite.  The full backend unit suite runs in
CI's "Test (Backend)" job.

Usage:
    python scripts/run_affected_tests.py [--list] [--base <ref>]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXCLUDE_NAMES: frozenset[str] = frozenset({"conftest.py", "__init__.py"})
MAX_TEST_FILES = 300
# Per-suite pytest --timeout, mirroring CI: ci.yml runs integration with
# --timeout=300 (testcontainer startup + migrations exceed the default 120).
SUITE_TIMEOUTS: dict[str, int] = {"integration": 300}


def _git(*args: str) -> str | None:
    """Run a git command and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _resolve_merge_base(base_ref: str) -> str | None:
    """Return the merge-base between *base_ref* and HEAD, or None."""
    return _git("merge-base", base_ref, "HEAD")


def _collect_changed_files(base: str) -> list[str]:
    """Return the list of files changed between *base* and HEAD."""
    merge_base = _resolve_merge_base(base)
    if merge_base is None:
        # Fallback: HEAD~1
        merge_base = _resolve_merge_base("HEAD~1")
        if merge_base is None:
            print(
                "notice: cannot determine merge-base with origin/main or HEAD~1 — skipping (CI runs the full suite)",
                file=sys.stderr,
            )
            return []
    raw = _git("diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}...HEAD")
    if not raw:
        return []
    return raw.splitlines()


def _stem(path: str) -> str:
    """Return filename stem (no extension) from a POSIX-style path."""
    return Path(path).stem


def _matches_stem(name: str, stem: str) -> bool:
    """Word-boundary stem match for candidate test filenames.

    ``me`` must match ``test_me.py`` / ``test_me_password.py`` but NOT
    ``test_metrics_ingest.py`` — a bare ``test_{stem}*`` prefix match
    over-selects unrelated suites and can drag a different test SUITE (which
    needs a different process environment) into the gate.  The next character
    after ``test_{stem}`` must be the end of the string or ``_``.
    """
    prefix = f"test_{stem}"
    if name == f"{prefix}.py":
        return True
    return name.startswith(f"{prefix}_")


def _suite_of(repo_relative_test_path: str) -> str:
    """Return the test-suite directory name for a repo-relative test path.

    ``backend/tests/unit/api/test_x.py`` -> ``unit``; ``backend/tests/integration/bdd/test_x.py``
    -> ``integration``; a test file directly under ``backend/tests/`` falls
    back to the ``tests`` pseudo-suite.
    """
    parts = Path(repo_relative_test_path).parts
    if len(parts) >= 4:
        return parts[2]
    return "tests"


def _group_by_suite(test_paths: list[str]) -> dict[str, list[str]]:
    """Group test paths by their suite directory, preserving sorted order."""
    groups: dict[str, list[str]] = {}
    for p in sorted(test_paths):
        groups.setdefault(_suite_of(p), []).append(p)
    return dict(sorted(groups.items()))


def _select_test_files(changed: list[str]) -> list[str]:
    """Derive the set of test file paths (repo-relative) to run."""
    selected: set[str] = set()
    backend_root = Path("backend")

    for f in changed:
        if not f.startswith("backend/"):
            continue
        name = Path(f).name
        if name in EXCLUDE_NAMES:
            continue

        if f.startswith("backend/tests/") and f.endswith(".py"):
            selected.add(f)
            continue

        if f.startswith("backend/src/") and f.endswith(".py"):
            stem = _stem(f)
            pattern = str(backend_root / "tests" / "**" / f"test_{stem}*.py")
            for match in sorted(backend_root.parent.glob(pattern)):
                rel = match.as_posix()
                if rel not in selected and _matches_stem(Path(rel).name, stem):
                    selected.add(rel)

    return sorted(selected)[:MAX_TEST_FILES]


def _run_pytest(test_paths: list[str], timeout: int = 120) -> int:
    """Run pytest on *test_paths* from the backend/ directory. Returns exit code."""
    cmd = [
        "uv",
        "run",
        "--no-sync",
        "pytest",
        "--tb=short",
        "-q",
        f"--timeout={timeout}",
        *test_paths,
    ]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    print("from:    backend/", file=sys.stderr)
    result = subprocess.run(cmd, cwd="backend", check=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    list_only = False
    base = "origin/main"

    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--list":
            list_only = True
        elif args[i] == "--base":
            i += 1
            if i >= len(args):
                print("error: --base requires a value", file=sys.stderr)
                return 2
            base = args[i]
        else:
            positional.append(args[i])
        i += 1

    changed = _collect_changed_files(base)
    if not changed:
        print(
            "no affected tests selected - skipping (CI runs the full suite)",
            file=sys.stderr,
        )
        return 0

    test_paths = _select_test_files(changed)
    if not test_paths:
        print(
            "no affected tests selected - skipping (CI runs the full suite)",
            file=sys.stderr,
        )
        return 0

    if list_only:
        for p in test_paths:
            print(p)
        return 0

    print(f"selected {len(test_paths)} test file(s):", file=sys.stderr)
    for p in test_paths:
        print(f"  {p}", file=sys.stderr)

    # Run each SUITE in its own pytest process. Conftest files set
    # process-wide environment (e.g. tests/bdd/conftest.py points DATABASE_URL
    # at sqlite at import time); mixing suites (BDD + integration) in one
    # process lets one conftest's environment leak into the other's tests and
    # fail them spuriously (mirrors CI, which runs each suite as a separate
    # job). Selection is unchanged — every affected test still runs.
    grouped = _group_by_suite(test_paths)
    worst = 0
    for suite, paths in grouped.items():
        out_paths = [p.removeprefix("backend/").removeprefix("./") for p in paths]
        print(f"\nsuite [{suite}] ({len(paths)} file(s)):", file=sys.stderr)
        for p in paths:
            print(f"  {p}", file=sys.stderr)
        code = _run_pytest(out_paths, timeout=SUITE_TIMEOUTS.get(suite, 120))
        if code != 0:
            worst = code
    return worst


if __name__ == "__main__":
    sys.exit(main())
