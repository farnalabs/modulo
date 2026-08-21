"""Verify that all skills from the devtools repo can be loaded successfully."""

import os
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="session")
def devtools_path() -> Path:
    env_path = os.environ.get("DEVTOOLS_PATH")
    if env_path:
        return Path(env_path).resolve()

    test_dir = Path(__file__).resolve().parent
    repo_root = test_dir.parents[3]
    sibling = (repo_root.parent / "devtools").resolve()
    if sibling.is_dir():
        return sibling

    alt = Path("/home/user/devtools")
    if alt.is_dir():
        return alt

    pytest.skip("Devtools repo not found. Set DEVTOOLS_PATH env var or place devtools as a sibling of modulo.")
    return Path()


@pytest.fixture(scope="session")
def skill_files(devtools_path: Path) -> list[Path]:
    skills_dir = devtools_path / "agents" / ".agents" / "skills"
    if not skills_dir.is_dir():
        skills_dir = devtools_path / ".agents" / "skills"
    if not skills_dir.is_dir():
        pytest.skip(f"Skills directory not found under {devtools_path}")
        return []
    files = sorted(skills_dir.rglob("SKILL.md"))
    assert len(files) > 0, f"No SKILL.md files found in {skills_dir}"
    return files


class TestDevtoolsSkills:
    def test_all_skills_have_valid_frontmatter(self, skill_files: list[Path]) -> None:
        for sf in skill_files:
            content = sf.read_text(encoding="utf-8")
            parts = content.split("---")
            assert len(parts) >= 3, f"{sf.parent.name}: missing YAML frontmatter delimiters"
            fm = yaml.safe_load(parts[1])
            assert fm is not None, f"{sf.parent.name}: frontmatter is empty"
            assert isinstance(fm, dict), f"{sf.parent.name}: frontmatter is not a mapping"
            assert "name" in fm, f"{sf.parent.name}: frontmatter missing 'name' field"
            name = fm["name"]
            assert isinstance(name, str), f"{sf.parent.name}: 'name' is empty"
            assert name.strip(), f"{sf.parent.name}: 'name' is empty"

    def test_all_skills_have_description(self, skill_files: list[Path]) -> None:
        for sf in skill_files:
            content = sf.read_text(encoding="utf-8")
            parts = content.split("---")
            assert len(parts) >= 3, f"{sf.parent.name}: missing YAML frontmatter delimiters"
            fm = yaml.safe_load(parts[1])
            assert fm is not None, f"{sf.parent.name}: invalid frontmatter"
            assert isinstance(fm, dict), f"{sf.parent.name}: invalid frontmatter"
            desc = fm.get("description")
            assert desc, f"{sf.parent.name}: frontmatter missing or empty 'description' field"
