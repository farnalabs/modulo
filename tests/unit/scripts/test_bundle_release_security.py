from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path
from subprocess import Popen

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


BUILD_CONSTRAINTS = {
    "pip": ("26.2.2", "sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"),
    "setuptools": ("84.2.1", "sha256:ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2"),
}
OPT_IN_DEADLINE_SECONDS = 600
SUBPROCESS_TIMEOUT_SECONDS = 120


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _locked_packages() -> dict[str, dict[str, object]]:
    return {
        package["name"]: package
        for package in tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))["package"]
        if package["name"] in EXPECTED_PACKAGES
    }


def _workflow_build_constraints(text: str) -> dict[str, tuple[str, str]]:
    match = re.search(r"cat > .*? <<'EOF'(?P<body>.*?)\n\s*EOF", text, re.DOTALL)
    assert match is not None
    return {
        name: (version, f"sha256:{digest}")
        for name, version, digest in re.findall(
            r"^\s*(pip|setuptools)==([^\s]+) --hash=sha256:([0-9a-f]{64})$",
            match.group("body"),
            re.MULTILINE,
        )
    }


def _sync_command(uv: str) -> list[str]:
    return [
        uv,
        "sync",
        "--frozen",
        "--no-dev",
        "--no-build",
        "--no-install-package",
        "ibm-cos-sdk",
        "--no-install-package",
        "ibm-cos-sdk-core",
        "--no-install-package",
        "ibm-cos-sdk-s3transfer",
    ]


def _python_in(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _site_packages(venv_root: Path) -> Path:
    candidates = list((venv_root / ("Lib" if os.name == "nt" else "lib")).glob("python*/site-packages"))
    assert len(candidates) == 1, f"expected one temporary venv site-packages directory, got {candidates}"
    return candidates[0]


def _popen(command: list[str], cwd: Path, env: dict[str, str], *, timeout: float) -> subprocess.Popen[bytes]:
    """Start a process whose caller enforces the bounded wait."""
    del timeout
    return Popen(  # noqa: S603
        command,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )


def _run_process(command: list[str], cwd: Path, deadline: float, env: dict[str, str]) -> None:
    remaining = deadline - time.monotonic()
    assert remaining > 0, "release security test exceeded its total wall-clock deadline"
    process_timeout = min(SUBPROCESS_TIMEOUT_SECONDS, remaining)
    process = _popen(command, cwd, env, timeout=process_timeout)
    try:
        process.wait(timeout=process_timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, 9)
        process.wait(timeout=10)
        raise AssertionError(f"subprocess exceeded its deadline: {command[0]}") from None
    assert process.returncode == 0, f"command failed with exit {process.returncode}: {command}"


def _direct_url(site_packages: Path, distribution: str) -> dict[str, object]:
    metadata = next(site_packages.glob(f"{distribution.replace('-', '_')}-*.dist-info"))
    direct_url = metadata / "direct_url.json"
    assert direct_url.is_file(), f"missing direct_url.json for {distribution}"
    return json.loads(direct_url.read_text(encoding="utf-8"))


def _verify_locked_archives(locked: dict[str, dict[str, object]]) -> None:
    """Verify the exact lockfile archives before any build or install command."""
    for package in EXPECTED_PACKAGES:
        sdist = locked[package]["sdist"]
        url = sdist["url"]
        expected_hash = sdist["hash"]
        expected_size = sdist["size"]
        with urllib.request.urlopen(url, timeout=SUBPROCESS_TIMEOUT_SECONDS) as response:  # noqa: S310
            archive = response.read()
        assert len(archive) == expected_size, f"unexpected size for {package} from {url}"
        assert f"sha256:{hashlib.sha256(archive).hexdigest()}" == expected_hash, (
            f"unexpected SHA-256 for {package} from {url}"
        )


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


@pytest.mark.release_bundle_security
def test_bundle_ibm_install_is_executable_and_provenance_verified() -> None:
    """Exercise the exact release boundary; opt in because it downloads/builds packages."""
    if os.environ.get("RUN_RELEASE_BUNDLE_SECURITY_TESTS") != "1":
        pytest.skip("set RUN_RELEASE_BUNDLE_SECURITY_TESTS=1 to run the release security test")
    uv = shutil.which("uv")
    assert uv is not None, "release security test requires uv"

    workflow_constraints = _workflow_build_constraints(_workflow_text())
    assert workflow_constraints == BUILD_CONSTRAINTS
    locked = _locked_packages()
    deadline = time.monotonic() + OPT_IN_DEADLINE_SECONDS
    with tempfile.TemporaryDirectory(prefix="modulo-release-security-") as temporary:
        root = Path(temporary)
        project = root / "backend"
        project.mkdir()
        shutil.copy2(BACKEND_ROOT / "pyproject.toml", project / "pyproject.toml")
        shutil.copy2(LOCKFILE, project / "uv.lock")
        shutil.copytree(BACKEND_ROOT / "src", project / "src")
        env = {
            **os.environ,
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
            "UV_CACHE_DIR": str(root / "uv-cache"),
        }
        _verify_locked_archives(locked)

        _run_process(_sync_command(uv), project, deadline, env)
        python = _python_in(project / ".venv")
        site_packages = _site_packages(project / ".venv")
        assert site_packages.is_relative_to(project / ".venv")

        constraints = root / "build-constraints.txt"
        constraint_lines = [
            f"{name}=={version} \\\n  --hash={digest}" for name, (version, digest) in BUILD_CONSTRAINTS.items()
        ]
        constraints.write_text("\n".join(constraint_lines) + "\n", encoding="utf-8")
        install_match = re.search(
            r"uv pip install --python \.venv/bin/python(?P<args>.*?)\n\n          BUNDLE=",
            _workflow_text(),
            re.DOTALL,
        )
        assert install_match is not None
        requirements = re.findall(
            r"'([^']+ @ https://files\.pythonhosted\.org/[^']+#sha256=[0-9a-f]{64})'",
            install_match.group(0),
        )
        install_args = [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--require-build-hashes",
            "--build-constraints",
            str(constraints),
            "--no-deps",
            *requirements,
        ]
        _run_process(install_args, project, deadline, env)

        for package in EXPECTED_PACKAGES:
            metadata = next(site_packages.glob(f"{package.replace('-', '_')}-*.dist-info"))
            assert metadata.is_relative_to(site_packages)
            assert (metadata / "METADATA").read_text(encoding="utf-8").find("Version: 2.14.3") >= 0
            provenance = _direct_url(site_packages, package)
            expected = locked[package]["sdist"]
            # The legacy setup.py source build records the archive URL but may
            # omit archive_info.hash. The lockfile bytes were verified above,
            # before any build/install command, so installed metadata is not the
            # authority for the archive digest.
            assert provenance["url"] == expected["url"]
            assert "archive_info" not in provenance or isinstance(provenance["archive_info"], dict)

        code = (
            "import importlib.metadata, pathlib, sys; "
            "site = pathlib.Path(sys.argv[1]).resolve(); "
            "dist = importlib.metadata.distribution('ibm-cos-sdk'); "
            "assert pathlib.Path(dist.locate_file('')).resolve().is_relative_to(site); "
            "assert importlib.metadata.version('ibm-cos-sdk') == '2.14.3'; "
            "import ibm_boto3, ibm_botocore, ibm_s3transfer; "
            "modules = (ibm_boto3, ibm_botocore, ibm_s3transfer); "
            "assert all(pathlib.Path(module.__file__).resolve().is_relative_to(site) for module in modules); "
            "paths = (entry for entry in sys.path if entry and 'site-packages' in entry); "
            "assert all(pathlib.Path(entry).resolve().is_relative_to(site) for entry in paths)"
        )
        _run_process([str(python), "-c", code, str(site_packages)], project, deadline, env)
