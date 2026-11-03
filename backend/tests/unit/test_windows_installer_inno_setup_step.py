"""Structural gate: windows-installer.yml must use the preinstalled Inno Setup (FAR-2331).

The windows-latest image ships a newer Inno Setup than the one the workflow
used to pin. Chocolatey refuses to replace a newer install, so both install
steps must probe for ISCC.exe first and only fall back to a plain choco
install when it is missing.
"""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "windows-installer.yml"


def _install_steps() -> list[tuple[str, str]]:
    doc = cast(dict[str, Any], yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    steps: list[tuple[str, str]] = []
    for job_name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "innosetup" in run.lower() or "ISCC.exe" in run:
                steps.append((job_name, run))
    return steps


def test_both_jobs_have_an_inno_setup_install_step() -> None:
    jobs = sorted(name for name, _ in _install_steps())
    assert jobs == ["build", "compile-check"]


@pytest.mark.parametrize("job_name", ["compile-check", "build"])
def test_pinned_downgrade_install_is_gone(job_name: str) -> None:
    run = dict(_install_steps())[job_name]
    assert "--allow-downgrade" not in run
    assert "--version=" not in run


@pytest.mark.parametrize("job_name", ["compile-check", "build"])
def test_preinstalled_iscc_is_probed_before_choco(job_name: str) -> None:
    run = dict(_install_steps())[job_name]
    probe = run.index("Find-Iscc")
    fallback = run.index("choco install innosetup")
    assert probe < fallback


@pytest.mark.parametrize("job_name", ["compile-check", "build"])
def test_iscc_directory_is_exported_to_github_path(job_name: str) -> None:
    run = dict(_install_steps())[job_name]
    assert "GITHUB_PATH" in run
