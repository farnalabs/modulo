from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCKFILE = REPO_ROOT / "backend" / "uv.lock"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bundle-release.yml"
EXPECTED_PACKAGES = {
    "ibm-cos-sdk",
    "ibm-cos-sdk-core",
    "ibm-cos-sdk-s3transfer",
}


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_locked_ibm_cos_archives_have_expected_urls_hashes_sizes_and_versions() -> None:
    packages = {
        package["name"]: package
        for package in tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))["package"]
        if package["name"] in EXPECTED_PACKAGES
    }
    assert set(packages) == EXPECTED_PACKAGES

    for package in packages.values():
        assert package["version"] == "2.14.3"
        assert package["sdist"]["url"].startswith("https://files.pythonhosted.org/")
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", package["sdist"]["hash"])
        assert package["sdist"]["size"] > 0


def test_bundle_keeps_global_no_build_and_only_allows_locked_ibm_cos_sources() -> None:
    text = _workflow_text()
    sync_match = re.search(r"uv sync --frozen --no-dev --no-build(?P<args>.*?)\n\n", text, re.DOTALL)
    assert sync_match is not None
    assert "--no-build" in sync_match.group(0)

    install_match = re.search(
        r"uv pip install --python \.venv/bin/python(?P<args>.*?)\n\n          BUNDLE=",
        text,
        re.DOTALL,
    )
    assert install_match is not None
    install_block = install_match.group(0)
    assert "--require-build-hashes" in install_block
    assert "--no-deps" in install_block
    assert "--no-build" not in install_block
    assert "--no-verify-hashes" not in install_block

    requirements = re.findall(r"'([^']+ @ https://files\.pythonhosted\.org/[^']+#sha256=[0-9a-f]{64})'", install_block)
    assert len(requirements) == len(EXPECTED_PACKAGES)
    locked = {
        package["name"]: package["sdist"]
        for package in tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))["package"]
        if package["name"] in EXPECTED_PACKAGES
    }
    for requirement in requirements:
        name, url = requirement.split(" @ ", 1)
        package = name.split("==", 1)[0]
        assert package in EXPECTED_PACKAGES
        assert name == package
        assert url == f"{locked[package]['url']}#{locked[package]['hash'].replace(':', '=')}"
    assert {requirement.split(" @ ", 1)[0] for requirement in requirements} == EXPECTED_PACKAGES


def test_bundle_excludes_the_three_source_packages_before_global_sync() -> None:
    text = _workflow_text()
    sync_block = re.search(r"uv sync --frozen --no-dev --no-build(?P<args>.*?)\n\n", text, re.DOTALL)
    assert sync_block is not None
    excluded = set(re.findall(r"--no-install-package ([^\s]+)", sync_block.group(0)))
    assert excluded == EXPECTED_PACKAGES
