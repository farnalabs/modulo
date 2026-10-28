from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path
from subprocess import Popen
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = REPO_ROOT / "backend"
LOCKFILE = BACKEND_ROOT / "uv.lock"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bundle-release.yml"
EXPECTED_PACKAGES = {
    "ibm-cos-sdk",
    "ibm-cos-sdk-core",
    "ibm-cos-sdk-s3transfer",
}
PACKAGE_MODULES = {
    "ibm-cos-sdk": "ibm_boto3",
    "ibm-cos-sdk-core": "ibm_botocore",
    "ibm-cos-sdk-s3transfer": "ibm_s3transfer",
}
BUILD_CONSTRAINTS = {
    "pip": ("26.2.2", "sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"),
    "setuptools": ("84.2.1", "sha256:ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2"),
}
OPT_IN_DEADLINE_SECONDS = 600
SUBPROCESS_TIMEOUT_SECONDS = 120
ARCHIVE_CHUNK_SIZE = 64 * 1024
EXPECTED_ARCHIVE_SIZES = {
    "ibm-cos-sdk": 58831,
    "ibm-cos-sdk-core": 1103637,
    "ibm-cos-sdk-s3transfer": 139594,
}


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _locked_packages() -> dict[str, dict[str, Any]]:
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


def _popen(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return Popen(command, **kwargs)  # noqa: S603


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    cleanup_timeout = 10
    if os.name == "nt":
        taskkill_path = shutil.which("taskkill")
        assert taskkill_path is not None, "Windows taskkill executable is required for subprocess cleanup"
        try:
            taskkill = subprocess.run(  # noqa: S603
                [taskkill_path, "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=cleanup_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssertionError(f"failed to terminate subprocess tree {process.pid}: {exc}") from exc
        if taskkill.returncode != 0 and process.poll() is None:
            details = taskkill.stderr.decode(errors="replace").strip()
            raise AssertionError(
                f"taskkill failed for subprocess tree {process.pid} with exit {taskkill.returncode}: {details}"
            )
        try:
            process.wait(timeout=cleanup_timeout)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"subprocess tree {process.pid} survived taskkill") from exc
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=cleanup_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError(f"failed to terminate subprocess group {process.pid}: {exc}") from exc


def _run_process(command: list[str], cwd: Path, deadline: float, env: dict[str, str]) -> None:
    remaining = deadline - time.monotonic()
    assert remaining > 0, "release security test exceeded its total wall-clock deadline"
    process_timeout = min(SUBPROCESS_TIMEOUT_SECONDS, remaining)
    process = _popen(command, cwd, env)
    try:
        process.wait(timeout=process_timeout)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        raise AssertionError(f"subprocess exceeded its deadline: {command[0]}") from None
    assert process.returncode == 0, f"command failed with exit {process.returncode}: {command}"


def _run_python_probe(
    python: Path,
    code: str,
    site_packages: Path,
    cwd: Path,
    deadline: float,
    env: dict[str, str],
) -> None:
    _run_process([str(python), "-c", code, str(site_packages)], cwd, deadline, env)


def _direct_url(site_packages: Path, distribution: str) -> dict[str, Any]:
    metadata = next(site_packages.glob(f"{distribution.replace('-', '_')}-*.dist-info"))
    direct_url = metadata / "direct_url.json"
    assert direct_url.is_file(), f"missing direct_url.json for {distribution}"
    return json.loads(direct_url.read_text(encoding="utf-8"))


def _verify_locked_archives(locked: dict[str, dict[str, Any]], deadline: float) -> None:
    """Verify exact lockfile archives before any build or install command."""
    for package in EXPECTED_PACKAGES:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "release security test exceeded its archive-download deadline"
        sdist = locked[package]["sdist"]
        response: Any = None
        try:
            response = urllib.request.urlopen(  # noqa: S310
                sdist["url"],
                timeout=min(SUBPROCESS_TIMEOUT_SECONDS, remaining),
            )
            digest = hashlib.sha256()
            size = 0
            response_socket = getattr(getattr(response, "fp", None), "raw", None)
            response_socket = getattr(response_socket, "_sock", None)
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, "release security test exceeded its archive-download deadline"
                if response_socket is not None and not response_socket._closed:
                    response_socket.settimeout(remaining)
                try:
                    chunk = response.read(ARCHIVE_CHUNK_SIZE)
                except TimeoutError as exc:
                    raise AssertionError("archive download exceeded its absolute deadline") from exc
                if time.monotonic() >= deadline:
                    raise AssertionError("archive download exceeded its absolute deadline")
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            assert size == sdist["size"], f"unexpected size for {package} from {sdist['url']}"
            assert f"sha256:{digest.hexdigest()}" == sdist["hash"], f"unexpected SHA-256 for {package}"
        finally:
            if response is not None:
                response.close()


def _workflow_sync_blocks(text: str) -> list[str]:
    pattern = (
        r"uv sync --frozen --no-dev --no-build.*?"
        r"(?=\n\s*(?:uv pip install|BUNDLE=|$))"
    )
    return [match.group(0) for match in re.finditer(pattern, text, re.DOTALL)]


def _workflow_install_requirements(text: str) -> list[str]:
    install_match = re.search(
        r"uv pip install --python \.venv/bin/python(?P<args>.*?)\n\s*BUNDLE=",
        text,
        re.DOTALL,
    )
    assert install_match is not None
    return re.findall(r"'([^']+ @ https://files\.pythonhosted\.org/[^']+#sha256=[0-9a-f]{64})'", install_match.group(0))


def test_locked_ibm_cos_archives_have_expected_exact_lock_entries() -> None:
    packages = _locked_packages()
    assert set(packages) == EXPECTED_PACKAGES
    for package in packages.values():
        assert package["version"] == "2.14.3"
        sdist = package["sdist"]
        assert re.fullmatch(r"https://files\.pythonhosted\.org/.+\.tar\.gz", sdist["url"])
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", sdist["hash"])
        assert isinstance(sdist["size"], int)
        assert sdist["size"] > 0


def test_bundle_archive_urls_and_hashes_match_lockfile_exactly() -> None:
    requirements = _workflow_install_requirements(_workflow_text())
    assert len(requirements) == len(EXPECTED_PACKAGES)
    locked = {package["name"]: package["sdist"] for package in _locked_packages().values()}
    actual: dict[str, dict[str, str]] = {}
    for requirement in requirements:
        name, direct_url = requirement.split(" @ ", 1)
        package = name.split("==", 1)[0]
        assert package in EXPECTED_PACKAGES
        assert name == package
        url, digest = direct_url.rsplit("#sha256=", 1)
        actual[package] = {"url": url, "hash": f"sha256:{digest}"}
    assert actual == {package: {"url": value["url"], "hash": value["hash"]} for package, value in locked.items()}
    assert {package: value["size"] for package, value in locked.items()} == EXPECTED_ARCHIVE_SIZES


def test_bundle_has_only_the_guarded_global_sync() -> None:
    text = _workflow_text()
    sync_blocks = _workflow_sync_blocks(text)
    assert len(sync_blocks) == 1
    sync_block = sync_blocks[0]
    assert "--no-build" in sync_block
    assert "--require-build-hashes" not in sync_block
    assert set(re.findall(r"--no-install-package ([^\s]+)", sync_block)) == EXPECTED_PACKAGES
    assert "uv sync --frozen --no-dev  # NOSONAR" not in text


def test_bundle_preserves_exact_build_constraints_and_safe_install_flags() -> None:
    text = _workflow_text()
    assert _workflow_build_constraints(text) == BUILD_CONSTRAINTS
    install_start = text.index("uv pip install --python")
    install_block = text[install_start : text.index("BUNDLE=", install_start)]
    assert "--require-build-hashes" in install_block
    assert "--no-deps" in install_block
    assert "--no-build" not in install_block
    assert "--no-verify-hashes" not in install_block


@pytest.mark.release_bundle_security
def test_bundle_ibm_install_is_executable_and_provenance_verified() -> None:
    """Exercise the exact release boundary; opt in because it downloads/builds packages."""
    if os.environ.get("RUN_RELEASE_BUNDLE_SECURITY_TESTS") != "1":
        pytest.skip("set RUN_RELEASE_BUNDLE_SECURITY_TESTS=1 to run the release security test")
    uv = shutil.which("uv")
    assert uv is not None, "release security test requires uv"

    text = _workflow_text()
    assert _workflow_build_constraints(text) == BUILD_CONSTRAINTS
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
        _verify_locked_archives(locked, deadline)
        _run_process(_sync_command(uv), project, deadline, env)
        python = _python_in(project / ".venv")
        site_packages = _site_packages(project / ".venv")
        assert site_packages.is_relative_to(project / ".venv")

        absent_code = (
            "import importlib.util, importlib.metadata, sys; "
            f"packages = {sorted(EXPECTED_PACKAGES)!r}; "
            f"modules = {sorted(PACKAGE_MODULES.values())!r}; "
            "assert all(importlib.util.find_spec(name) is None for name in modules); "
            "assert all("
            "next((dist for dist in importlib.metadata.distributions() "
            "if dist.metadata['Name'] == name), None) is None for name in packages)"
        )
        _run_python_probe(python, absent_code, site_packages, project, deadline, env)

        constraints = root / "build-constraints.txt"
        constraint_lines = [
            f"{name}=={version} \\\n  --hash={digest}" for name, (version, digest) in BUILD_CONSTRAINTS.items()
        ]
        constraints.write_text("\n".join(constraint_lines) + "\n", encoding="utf-8")
        requirements = _workflow_install_requirements(text)
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
            assert f"Name: {package}\n" in (metadata / "METADATA").read_text(encoding="utf-8")
            assert "Version: 2.14.3\n" in (metadata / "METADATA").read_text(encoding="utf-8")
            provenance = _direct_url(site_packages, package)
            assert provenance["url"] == locked[package]["sdist"]["url"]

        present_code = (
            "import importlib, importlib.metadata, pathlib, sys; "
            "site = pathlib.Path(sys.argv[1]).resolve(); "
            f"packages = {sorted(EXPECTED_PACKAGES)!r}; "
            f"modules = {sorted(PACKAGE_MODULES.values())!r}; "
            "assert all(importlib.metadata.version(name) == '2.14.3' for name in packages); "
            "assert all("
            "pathlib.Path(importlib.import_module(name).__file__).resolve().is_relative_to(site) "
            "for name in modules); "
            "assert all("
            "pathlib.Path(importlib.metadata.distribution(name).locate_file('')).resolve().is_relative_to(site) "
            "for name in packages)"
        )
        _run_python_probe(python, present_code, site_packages, project, deadline, env)
