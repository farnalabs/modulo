"""Pytest port of tools/tests/test-pre-commit-config.ps1 (FAR-300).

Verifies the pre-commit hooks are cross-platform Python/uv entries and are
never wrapped in Bash.
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
    assert re.search(r"(?m)^\s*entry:\s*uv --directory backend run --no-sync lint-imports\s*$", config)


def test_no_bash_wrapped_uv_hooks():
    config = _config()
    assert not re.search(r"(?m)^\s*entry:\s*(?:/bin/)?bash\b[^\r\n]*\buv\b", config)


def test_migration_collision_check_runs_through_cross_platform_python_script():
    config = _config()
    assert re.search(
        r"(?m)^\s*entry:\s*uv run --project backend --no-sync python scripts/run_check_migration_heads\.py\s*$",
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
