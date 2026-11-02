"""Pytest port of tools/tests/test-pre-commit-config.ps1 (FAR-300).

Verifies the pre-commit hooks are cross-platform Python/uv entries and are
never wrapped in Bash, and (FAR-2308) that every `uv ... run` invocation is
`--frozen` so no hook can rewrite backend/uv.lock as a side effect.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[3])


def _config() -> str:
    with Path(REPO_ROOT, ".pre-commit-config.yaml").open(encoding="utf-8") as fh:
        return fh.read()


def test_import_linter_runs_through_backend_project_environment():
    config = _config()
    assert re.search(r"(?m)^\s*entry:\s*uv --directory backend run --no-sync --frozen lint-imports\s*$", config)


def test_no_bash_wrapped_uv_hooks():
    config = _config()
    assert not re.search(r"(?m)^\s*entry:\s*(?:/bin/)?bash\b[^\r\n]*\buv\b", config)


def test_migration_collision_check_runs_through_cross_platform_python_script():
    config = _config()
    assert re.search(
        r"(?m)^\s*entry:\s*uv run --project backend --no-sync --frozen python "
        r"scripts/run_check_migration_heads\.py\s*$",
        config,
    )
    assert Path(REPO_ROOT, "scripts", "run_check_migration_heads.py").is_file()


def test_check_c1_chars_files_pattern_matches_workflow_files():
    """Regression guard (FAR-1350): a doubled-backslash pattern (``\\\\.github``)
    requires a literal backslash in the path, so the hook never fired. The
    pattern must stay single-backslash and match real workflow paths."""
    config = _config()
    lines = config.splitlines()
    id_indexes = [i for i, line in enumerate(lines) if line.strip() == "- id: check-c1-chars"]
    assert id_indexes, "check-c1-chars hook missing from .pre-commit-config.yaml"
    pattern_lines = [line for line in lines[id_indexes[0] + 1 :] if line.strip().startswith("files:")]
    assert pattern_lines, "check-c1-chars hook has no files: pattern"
    pattern = pattern_lines[0].split("files: ", 1)[1].strip()
    assert "\\\\" not in pattern
    compiled = re.compile(pattern)
    assert compiled.search(".github/workflows/ci.yml")
    assert compiled.search(".github/workflows/ci.yaml")
    assert compiled.search(".github/workflows/sub/deploy.yml")
    assert not compiled.search("backend/src/modulo/api/routes/runs.py")


def test_every_uv_run_invocation_is_frozen():
    """FAR-2308: no hook may rewrite backend/uv.lock as a side effect.

    ``uv run`` / ``uv --directory backend run`` updates ``backend/uv.lock`` when
    it has drifted from ``backend/pyproject.toml``. During a commit gate that
    mutates the tree between pre-commit's stash and restore, which aborts the
    commit and every later push from a branch based on the stale tree.
    ``--frozen`` forbids the lockfile write; it does not change what the hook
    runs. Pre-push entries carry a second invocation after `` -- `` (the gated
    command), so both are checked.
    """
    config = _config()
    entry_lines = [line.strip() for line in config.splitlines() if line.strip().startswith("entry:")]
    offenders: list[str] = []
    for line in entry_lines:
        if not re.search(r"(?:^|\s)uv(?:\s|$)", line):
            continue
        invocations = [segment for segment in line.split(" -- ") if re.search(r"(?:^|\s)uv(?:\s|$).*\brun\b", segment)]
        if any("--frozen" not in segment for segment in invocations):
            offenders.append(line)
    assert not offenders, (
        "pre-commit hook uv invocations must pass `--frozen` (FAR-2308) so a stale "
        "backend/uv.lock is never rewritten mid-gate:\n" + "\n".join(offenders)
    )
