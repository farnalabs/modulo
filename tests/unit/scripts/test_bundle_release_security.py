from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import venv
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
LOCKFILE = BACKEND_ROOT / "uv.lock"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bundle-release.yml"
EXPECTED_PACKAGES = {
    "ibm-cos-sdk",
    "ibm-cos-sdk-core",
    "ibm-cos-sdk-s3transfer",
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "release_bundle_security: opt-in executable release supply-chain verification",
    )


BUILD_WHEELS = {
    "pip": {
        "version": "26.2.2",
        "url": "https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl",
        "hash": "sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70",
        "size": 1819850,
    },
    "setuptools": {
        "version": "84.2.1",
        "url": "https://files.pythonhosted.org/packages/ef/55/6dc6d6bb953bdfc52519a0059592c52b6e3b8ad51cf8256dffceb55b0610/setuptools-84.2.1-py3-none-any.whl",
        "hash": "sha256:ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2",
        "size": 876263,
    },
}


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _locked_packages() -> dict[str, dict[str, object]]:
    return {
        package["name"]: package
        for package in tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))["package"]
        if package["name"] in EXPECTED_PACKAGES
    }


def _download_and_verify(url: str, expected_hash: str, expected_size: int, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "modulo-release-bundle-security-test"})  # noqa: S310
    digest = hashlib.sha256()
    size = 0
    with urlopen(request, timeout=30) as response, destination.open("wb") as output:  # noqa: S310
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
            output.write(chunk)
    assert size == expected_size, f"{destination.name}: size {size} != locked {expected_size}"
    assert digest.hexdigest() == expected_hash.removeprefix("sha256:"), f"{destination.name}: SHA-256 mismatch"


def _python_in(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _site_packages(venv_root: Path) -> Path:
    candidates = list((venv_root / ("Lib" if os.name == "nt" else "lib")).glob("python*/site-packages"))
    assert len(candidates) == 1, f"expected one temporary venv site-packages directory, got {candidates}"
    return candidates[0]


def _run_uv(uv: str, python: Path, *arguments: str, timeout: int = 300) -> None:
    command = [uv, "pip", "install", "--python", str(python), *arguments]
    subprocess.run(command, cwd=python.parent.parent, check=True, timeout=timeout)  # noqa: S603


def test_locked_ibm_cos_archives_have_expected_urls_hashes_sizes_and_versions() -> None:
    packages = _locked_packages()
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
    locked = {package["name"]: package["sdist"] for package in _locked_packages().values()}
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


def test_bundle_ibm_install_is_executable_and_offline_after_global_sync() -> None:
    if os.environ.get("RUN_RELEASE_BUNDLE_SECURITY_TESTS") != "1":
        pytest.skip("set RUN_RELEASE_BUNDLE_SECURITY_TESTS=1 to run the release security test")
    """Exercise the release boundary; opt in because it downloads/builds packages."""
    uv = shutil.which("uv")
    global_venv = BACKEND_ROOT / ".venv"
    if uv is None or not global_venv.is_dir():
        pytest.skip("requires uv and the global backend .venv produced by uv sync")

    locked = _locked_packages()
    with tempfile.TemporaryDirectory(prefix="modulo-release-security-") as temporary:
        root = Path(temporary)
        downloads = root / "downloads"
        downloads.mkdir()
        for package in locked.values():
            archive = package["sdist"]
            destination = downloads / Path(archive["url"]).name
            _download_and_verify(archive["url"], archive["hash"], archive["size"], destination)
        for wheel in BUILD_WHEELS.values():
            destination = downloads / Path(wheel["url"]).name
            _download_and_verify(wheel["url"], wheel["hash"], wheel["size"], destination)

        isolated_venv = root / "venv"
        venv.EnvBuilder(with_pip=False, clear=True, symlinks=True).create(isolated_venv)
        python = _python_in(isolated_venv)
        global_site = next(global_venv.glob("lib/python*/site-packages"), None)
        assert global_site is not None, "global backend .venv has no site-packages directory"
        _site_packages(isolated_venv).joinpath("global_sync.pth").write_text(f"{global_site}\n", encoding="utf-8")

        constraints = root / "build-constraints.txt"
        constraints.write_text(
            "\n".join(
                f"{name}=={wheel['version']} \\\n  --hash={wheel['hash']}" for name, wheel in BUILD_WHEELS.items()
            )
            + "\n",
            encoding="utf-8",
        )
        _run_uv(
            uv,
            python,
            "--no-index",
            "--find-links",
            str(downloads),
            "--require-hashes",
            "--no-deps",
            "-r",
            str(constraints),
        )

        _run_uv(
            uv,
            python,
            "--no-index",
            "--find-links",
            str(downloads),
            "--require-build-hashes",
            "--build-constraints",
            str(constraints),
            "--no-deps",
            *(str(downloads / Path(package["sdist"]["url"]).name) for package in locked.values()),
        )

        code = (
            "import importlib.metadata, sys; "
            "assert importlib.metadata.version('ibm-cos-sdk') == '2.14.3'; "
            "import ibm_boto3, ibm_botocore, ibm_s3transfer; "
            "names = ('jmespath', 'python-dateutil', 'requests', 'urllib3'); "
            "assert all("
            "str(importlib.metadata.distribution(name).locate_file('')).startswith(sys.argv[1]) "
            "for name in names)"
        )
        subprocess.run(  # noqa: S603
            [str(python), "-c", code, str(global_site)], check=True, timeout=30
        )
