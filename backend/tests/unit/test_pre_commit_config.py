"""Pytest port of tools/tests/test-pre-commit-config.ps1 (FAR-300).

Verifies the pre-commit hooks are cross-platform Python/uv entries and are
never wrapped in Bash.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[3])


def _config() -> str:
    with open(os.path.join(REPO_ROOT, ".pre-commit-config.yaml"), encoding="utf-8") as fh:
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
